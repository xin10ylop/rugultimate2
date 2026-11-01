//! Operator fingerprints and point-in-time track records.
//!
//! A launch gets several fingerprint keys (creator wallet, funder, funder+amount, distinctive
//! dev-buy amount, coin name). Each key accumulates the realistic-fill outcome vectors of earlier
//! launches, but only after their outcome window has fully elapsed (no look-ahead).

use crate::fills::Policy;
use crate::solana::Pubkey;
use std::collections::HashMap;

#[derive(Clone, Debug)]
pub struct Matured {
    pub slot0: u64,
    pub returns: Vec<f64>,
}

#[derive(Default)]
pub struct Registry {
    by_key: HashMap<String, Vec<Matured>>,
    pub funder_count: HashMap<Pubkey, u32>,
}

#[derive(Clone, Debug)]
pub struct Choice {
    pub key: String,
    pub policy_idx: usize,
    pub n: usize,
    pub mean: f64,
    pub lb: f64,
}

pub struct Gate {
    pub min_n: usize,
    pub min_mean: f64,
    pub lb_z: f64,
    pub max_key_history: usize,
}

impl Default for Gate {
    fn default() -> Self {
        Gate { min_n: 5, min_mean: 0.05, lb_z: 1.0, max_key_history: 60 }
    }
}

pub const HUB_FUNDER: u32 = 40;

pub fn fingerprint_keys(creator: &Pubkey, funder: Option<(Pubkey, u64)>, dev_buy_lamports: u64, mayhem: bool, name: &str,
                        funder_seen: u32) -> Vec<String> {
    let mut k = vec![format!("creator:{creator}")];
    if let Some((f, amt)) = funder {
        if funder_seen <= HUB_FUNDER {
            k.push(format!("funder:{f}"));
        }
        k.push(format!("funder_amt:{f}:{}", amt / 100_000));
    }
    let q = (dev_buy_lamports as f64 / 1e5).round() as u64;
    if q > 0 && q % 100 != 0 {
        k.push(format!("devbuy:{q}:{}", mayhem as u8));
    }
    let n = name.trim().to_lowercase();
    if n.chars().count() >= 3 {
        k.push(format!("name:{n}"));
    }
    k
}

impl Registry {
    pub fn add(&mut self, keys: &[String], m: Matured, cap: usize) {
        for k in keys {
            let v = self.by_key.entry(k.clone()).or_default();
            v.push(m.clone());
            if v.len() > cap {
                v.remove(0);
            }
        }
    }

    /// Best (key, policy) whose earlier evidence passes the gate, or None.
    pub fn choose(&self, keys: &[String], policies: &[Policy], allowed: &[usize], g: &Gate) -> Option<Choice> {
        let mut best: Option<Choice> = None;
        for k in keys {
            let hist = match self.by_key.get(k) {
                Some(h) if h.len() >= g.min_n => h,
                _ => continue,
            };
            let n = hist.len();
            for &pi in allowed {
                if pi >= policies.len() {
                    continue;
                }
                let vals: Vec<f64> = hist.iter().map(|m| m.returns[pi]).collect();
                let mean = vals.iter().sum::<f64>() / n as f64;
                let var = vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0).max(1.0);
                let lb = mean - g.lb_z * var.sqrt() / (n as f64).sqrt();
                if mean > g.min_mean && lb > 0.0 && best.as_ref().map_or(true, |b| lb > b.lb) {
                    best = Some(Choice { key: k.clone(), policy_idx: pi, n, mean, lb });
                }
            }
        }
        best
    }
}
