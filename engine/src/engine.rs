//! Core state machine: ordered events in, decisions out.
//!
//! * keeps every live coin's tape (post-trade curve states)
//! * remembers the last SOL transfer into every wallet (funding graph, recent window)
//! * at a coin's trigger age, computes fingerprint keys and asks the registry for a playbook
//! * after the outcome horizon, scores the launch with the realistic-fill grid and adds it to the
//!   track records of its keys (this is the only way the engine learns: strictly after the fact)

use crate::block::{Ev, Located};
use crate::fills::{grid_avg, policy_grid, Costs, Policy, Tick};
use crate::operators::{fingerprint_keys, Choice, Gate, Matured, Registry};
use crate::pump::{PumpEvent, MAYHEM_AGENT};
use crate::solana::{pk, Pubkey};
use rand::Rng;
use serde::Serialize;
use std::collections::HashMap;

pub const HORIZON: u64 = 2850;

#[derive(Clone, Debug)]
pub struct FundRec {
    pub from: Pubkey,
    pub lamports: u64,
    pub slot: u64,
}

pub struct CoinState {
    pub slot0: u64,
    pub creator: Pubkey,
    pub token_program: Pubkey,
    pub mayhem: bool,
    pub name: String,
    pub ticks: Vec<Tick>,
    pub users: Vec<Pubkey>,
    pub fee_rate: f64,
    pub dev_buy: u64,
    pub funder: Option<(Pubkey, u64)>,
    pub trig_row: Option<usize>,
    pub keys: Vec<String>,
    pub last_slot: u64,
}

#[derive(Clone, Debug, Serialize)]
pub struct Decision {
    pub mint: String,
    pub slot: u64,
    pub trig_row: usize,
    pub key: String,
    pub policy: (f64, f64, u64),
    pub n_prev: usize,
    pub exp: f64,
    pub lb: f64,
    pub mayhem: bool,
}

pub struct Engine {
    pub coins: HashMap<Pubkey, CoinState>,
    pub funding: HashMap<Pubkey, FundRec>,
    pub registry: Registry,
    pub policies: Vec<Policy>,
    pub allowed: Vec<usize>,
    pub gate: Gate,
    pub costs: Costs,
    pub size: f64,
    pub trig_age: u64,
    pub head_slot: u64,
    agent: Pubkey,
    pub stats: HashMap<&'static str, u64>,
}

impl Engine {
    pub fn new(size: f64, gate: Gate, costs: Costs) -> Self {
        let policies = policy_grid();
        let allowed = (0..policies.len()).collect();
        Engine { coins: HashMap::new(), funding: HashMap::new(), registry: Registry::default(), policies, allowed, gate, costs,
                 size, trig_age: 2, head_slot: 0, agent: pk(MAYHEM_AGENT), stats: HashMap::new() }
    }

    fn bump(&mut self, k: &'static str) {
        *self.stats.entry(k).or_default() += 1;
    }

    /// Process one ordered event. Returns a Decision if a playbook fires on this event.
    pub fn on_event(&mut self, l: &Located) -> Option<Decision> {
        self.head_slot = self.head_slot.max(l.slot);
        match &l.ev {
            Ev::Transfer(t) => {
                self.funding.insert(t.to, FundRec { from: t.from, lamports: t.lamports, slot: l.slot });
                None
            }
            Ev::FailedPump { .. } => None,
            Ev::Pump(PumpEvent::Create(c)) => {
                if !c.quote_is_sol {
                    return None;
                }
                let funder = self.funding.get(&c.creator).filter(|f| f.from != c.creator).map(|f| (f.from, f.lamports));
                if let Some((f, _)) = funder {
                    *self.registry.funder_count.entry(f).or_default() += 1;
                }
                self.coins.insert(c.mint, CoinState { slot0: l.slot, creator: c.creator, token_program: c.token_program, mayhem: c.mayhem,
                    name: c.name.clone(), ticks: Vec::with_capacity(64), users: Vec::with_capacity(64), fee_rate: 0.0125,
                    dev_buy: 0, funder, trig_row: None, keys: vec![], last_slot: l.slot });
                self.bump("creates");
                None
            }
            Ev::Pump(PumpEvent::Trade(t)) => {
                let agent = self.agent;
                let c = self.coins.get_mut(&t.mint)?;
                if !t.quote_is_sol || t.sol == 0 {
                    return None;
                }
                c.ticks.push(Tick { slot: l.slot, vsol: t.vsol as f64, vtok: t.vtok as f64 });
                c.users.push(t.user);
                c.last_slot = l.slot;
                if t.user != agent && t.sol > 0 && (t.fee + t.creator_fee) > 0 {
                    c.fee_rate = (t.fee + t.creator_fee) as f64 / t.sol as f64;
                }
                let rel = l.slot - c.slot0;
                if rel == 0 && t.is_buy && t.user == c.creator {
                    c.dev_buy += t.sol;
                }
                if c.trig_row.is_none() && rel >= self.trig_age {
                    let j = c.ticks.len() - 1;
                    c.trig_row = Some(j);
                    let fseen = c.funder.map(|(f, _)| *self.registry.funder_count.get(&f).unwrap_or(&0)).unwrap_or(0);
                    c.keys = fingerprint_keys(&c.creator, c.funder, c.dev_buy, c.mayhem, &c.name, fseen);
                    let keys = c.keys.clone();
                    let mayhem = c.mayhem;
                    let choice: Option<Choice> = self.registry.choose(&keys, &self.policies, &self.allowed, &self.gate);
                    if let Some(ch) = choice {
                        let p = self.policies[ch.policy_idx];
                        self.bump("decisions");
                        return Some(Decision { mint: t.mint.to_b58(), slot: l.slot, trig_row: j, key: ch.key, policy: (p.tp, p.sl, p.max_slots),
                                               n_prev: ch.n, exp: ch.mean, lb: ch.lb, mayhem });
                    }
                }
                None
            }
            Ev::Pump(PumpEvent::Complete { .. }) => None,
        }
    }

    /// Score coins whose outcome horizon elapsed; add them to the track records; free memory.
    /// Returns (mint, per-policy returns) for matured coins so paper trades can be settled.
    pub fn mature(&mut self, rng: &mut impl Rng) -> Vec<(Pubkey, Vec<f64>)> {
        let head = self.head_slot;
        let ready: Vec<Pubkey> = self.coins.iter().filter(|(_, c)| head >= c.slot0 + HORIZON).map(|(m, _)| *m).collect();
        let mut out = vec![];
        for m in ready {
            let c = self.coins.remove(&m).unwrap();
            if let Some(j) = c.trig_row {
                let r = grid_avg(&c.ticks, j, self.size, c.fee_rate, &self.costs, &self.policies, rng.gen_range(5..14), rng.gen_range(0..3));
                self.registry.add(&c.keys, Matured { slot0: c.slot0, returns: r.clone() }, self.gate.max_key_history);
                out.push((m, r));
                self.bump("matured");
            }
        }
        // funding memory: keep ~6 hours
        if self.funding.len() > 2_000_000 {
            let cutoff = head.saturating_sub(100_000);
            self.funding.retain(|_, f| f.slot >= cutoff);
        }
        out
    }
}
