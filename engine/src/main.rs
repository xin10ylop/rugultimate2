//! rugbot — operator-playbook engine for pump.fun.
//!
//! Modes
//!   replay   : run the engine over a historical slot range (Rust backtest, identical logic to paper)
//!   paper    : follow the chain tip, make decisions, settle them with the never-first fill model
//!   dry-buy  : build a real buy transaction for a mint and run simulateTransaction (no send)
//!   live     : (guarded) send real transactions — see docs/LIVE.md before using

mod block;
mod engine;
mod fills;
mod operators;
mod pump;
mod risk;
mod rpc;
mod solana;

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use engine::{Decision, Engine};
use fills::Costs;
use operators::Gate;
use pump::{Decoder, GlobalCfg, PumpAccounts};
use risk::{Risk, RiskCfg};
use rpc::Rpc;
use serde_json::json;
use std::collections::{BTreeMap, HashMap};
use std::io::Write;
use std::sync::Arc;
use tokio::sync::Semaphore;

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    Replay {
        #[arg(long, default_value = "https://solana-rpc.publicnode.com/")]
        rpc: String,
        #[arg(long)]
        start: u64,
        #[arg(long)]
        end: u64,
        #[arg(long, default_value_t = 8)]
        workers: usize,
        #[arg(long, default_value = "journal_replay.jsonl")]
        journal: String,
        #[arg(long, default_value_t = 0.25)]
        size: f64,
        #[arg(long, default_value_t = 5)]
        min_n: usize,
        #[arg(long, default_value_t = 0.05)]
        min_mean: f64,
    },
    Paper {
        #[arg(long, default_value = "https://solana-rpc.publicnode.com/")]
        rpc: String,
        #[arg(long, default_value_t = 8)]
        workers: usize,
        #[arg(long, default_value = "journal_paper.jsonl")]
        journal: String,
        #[arg(long, default_value_t = 0.25)]
        size: f64,
        #[arg(long, default_value_t = 5)]
        min_n: usize,
        #[arg(long, default_value_t = 0.05)]
        min_mean: f64,
        /// warm up the track records over this many slots before the tip
        #[arg(long, default_value_t = 30000)]
        warmup: u64,
    },
    DryBuy {
        #[arg(long, default_value = "https://solana-rpc.publicnode.com/")]
        rpc: String,
        #[arg(long)]
        mint: String,
        #[arg(long)]
        creator: String,
        #[arg(long, default_value_t = false)]
        mayhem: bool,
        #[arg(long)]
        keypair: Option<String>,
        #[arg(long, default_value_t = 0.01)]
        sol: f64,
        /// simulate as this existing funded wallet (no signature, nothing is sent)
        #[arg(long)]
        as_wallet: Option<String>,
    },
}

struct Journal {
    f: std::fs::File,
}
impl Journal {
    fn open(p: &str) -> Result<Self> {
        Ok(Journal { f: std::fs::OpenOptions::new().create(true).append(true).open(p)? })
    }
    fn write(&mut self, v: serde_json::Value) {
        let _ = writeln!(self.f, "{}", v);
        let _ = self.f.flush();
    }
}

/// Fetch blocks [from, to] concurrently, yield them in slot order.
async fn fetch_range(rpc: &Rpc, from: u64, to: u64, workers: usize) -> BTreeMap<u64, Option<serde_json::Value>> {
    let sem = Arc::new(Semaphore::new(workers));
    let mut handles = vec![];
    for s in from..=to {
        let rpc = rpc.clone();
        let sem = sem.clone();
        handles.push(tokio::spawn(async move {
            let _p = sem.acquire().await.unwrap();
            for attempt in 0..8 {
                match rpc.block(s).await {
                    Ok(b) => return (s, b),
                    Err(e) => {
                        let msg = e.to_string();
                        if msg.contains("-32004") || msg.contains("429") || attempt < 7 {
                            tokio::time::sleep(std::time::Duration::from_millis(300 * (attempt + 1) as u64)).await;
                            continue;
                        }
                    }
                }
            }
            (s, None)
        }));
    }
    let mut out = BTreeMap::new();
    for h in handles {
        if let Ok((s, b)) = h.await {
            out.insert(s, b);
        }
    }
    out
}

struct PaperBook {
    open: HashMap<String, (Decision, i64)>,
    pnl: f64,
    n: u64,
    wins: u64,
}

fn settle(book: &mut PaperBook, eng: &Engine, risk: &mut Risk, j: &mut Journal, matured: &[(solana::Pubkey, Vec<f64>)], size: f64) {
    for (m, rets) in matured {
        if let Some((d, t0)) = book.open.remove(&m.to_b58()) {
            let idx = eng.policies.iter().position(|p| (p.tp, p.sl, p.max_slots) == d.policy).unwrap_or(0);
            let r = rets[idx];
            book.pnl += r * size;
            book.n += 1;
            if r > 0.0 {
                book.wins += 1;
            }
            risk.on_close(&d.key, r * size);
            j.write(json!({"type": "settle", "mint": d.mint, "key": d.key, "policy": d.policy, "ret": r, "pnl_sol": r * size,
                           "cum_pnl_sol": book.pnl, "n": book.n, "win_rate": book.wins as f64 / book.n as f64, "decided_unix": t0}));
        }
    }
}

async fn run_range(eng: &mut Engine, rpc: &Rpc, from: u64, to: u64, workers: usize, book: &mut PaperBook, risk: &mut Risk,
                   j: &mut Journal, size: f64, trade: bool) -> Result<u64> {
    let dec = Decoder::default();
    let mut rng = rand::thread_rng();
    let mut s = from;
    let chunk = 400u64;
    while s <= to {
        let e = (s + chunk - 1).min(to);
        let blocks = fetch_range(rpc, s, e, workers).await;
        for (slot, b) in blocks {
            if let Some(b) = b {
                let now = b["blockTime"].as_i64().unwrap_or(0);
                for l in block::parse_block(slot, &b, &dec, 10_000_000) {
                    if let Some(d) = eng.on_event(&l) {
                        if !trade {
                            continue;
                        }
                        match risk.allow(&d.key, now) {
                            Ok(()) => {
                                j.write(json!({"type": "enter", "decision": d, "unix": now}));
                                book.open.insert(d.mint.clone(), (d, now));
                            }
                            Err(why) => j.write(json!({"type": "skip", "mint": d.mint, "key": d.key, "why": why})),
                        }
                    }
                }
            }
            eng.head_slot = eng.head_slot.max(slot);
            let matured = eng.mature(&mut rng);
            settle(book, eng, risk, j, &matured, size);
        }
        eprintln!("slots {s}..{e} coins_live={} stats={:?} paper: n={} pnl={:+.3} SOL", eng.coins.len(), eng.stats, book.n, book.pnl);
        s = e + 1;
    }
    Ok(to)
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Replay { rpc, start, end, workers, journal, size, min_n, min_mean } => {
            let rpc = Rpc::new(&rpc);
            let mut eng = Engine::new(size, Gate { min_n, min_mean, ..Default::default() }, Costs::default());
            let mut risk = Risk::new(RiskCfg { size_sol: size, max_open: 1000, daily_loss_limit_sol: 1e9, ..Default::default() });
            let mut j = Journal::open(&journal)?;
            let mut book = PaperBook { open: HashMap::new(), pnl: 0.0, n: 0, wins: 0 };
            run_range(&mut eng, &rpc, start, end, workers, &mut book, &mut risk, &mut j, size, true).await?;
            println!("replay done: trades={} pnl={:+.4} SOL win_rate={:.2}", book.n, book.pnl, book.wins as f64 / book.n.max(1) as f64);
        }
        Cmd::Paper { rpc, workers, journal, size, min_n, min_mean, warmup } => {
            let rpc = Rpc::new(&rpc);
            let mut eng = Engine::new(size, Gate { min_n, min_mean, ..Default::default() }, Costs::default());
            let mut risk = Risk::new(RiskCfg { size_sol: size, ..Default::default() });
            let mut j = Journal::open(&journal)?;
            let mut book = PaperBook { open: HashMap::new(), pnl: 0.0, n: 0, wins: 0 };
            let tip = rpc.slot().await?;
            eprintln!("warming up track records over {warmup} slots before {tip}");
            run_range(&mut eng, &rpc, tip - warmup, tip - 1, workers, &mut book, &mut risk, &mut j, size, false).await?;
            let mut next = tip;
            loop {
                let tip = rpc.slot().await.unwrap_or(next);
                if tip > next + 2 {
                    next = run_range(&mut eng, &rpc, next, tip - 2, workers, &mut book, &mut risk, &mut j, size, true).await? + 1;
                } else {
                    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
                }
            }
        }
        Cmd::DryBuy { rpc, mint, creator, mayhem, keypair, sol, as_wallet } => {
            let rpc = Rpc::new(&rpc);
            let accts = PumpAccounts::default();
            let gdata = rpc.account_data(&accts.global.to_b58()).await?;
            let g = GlobalCfg::parse(&gdata)?;
            println!("global: {} fee recipients, {} reserved, {} buyback", g.fee_recipients.len(), g.reserved_fee_recipients.len(), g.buyback_recipients.len());
            let kp = match keypair {
                Some(p) => solana::Keypair::from_file(&p)?,
                None => solana::Keypair::generate(),
            };
            let user = match &as_wallet {
                Some(w) => solana::Pubkey::from_str(w)?,
                None => kp.pubkey(),
            };
            let mint_pk = solana::Pubkey::from_str(&mint)?;
            let token_program = solana::pk(pump::TOKEN_2022);
            let coin = pump::CoinKeys { mint: mint_pk, creator: solana::Pubkey::from_str(&creator)?, token_program, mayhem };
            let ixs = vec![
                solana::ix_cu_limit(120_000),
                solana::ix_cu_price(50_000),
                solana::ix_create_ata_idempotent(user, user, mint_pk, token_program),
                accts.ix_buy_exact_sol_in(&g, &coin, &user, (sol * 1e9) as u64, 1, 0),
            ];
            let bh = rpc.latest_blockhash().await?;
            let tx = if as_wallet.is_some() { solana::build_unsigned_tx(user, &ixs, &bh)? } else { solana::build_legacy_tx(&kp, &ixs, &bh)? };
            println!("tx bytes: {} (limit 1232)", tx.len());
            if tx.len() > 1232 {
                bail!("transaction too large");
            }
            let (err, logs) = rpc.simulate(&tx).await?;
            println!("simulate err: {err}");
            for l in logs.iter().take(40) {
                println!("  {l}");
            }
        }
    }
    Ok(())
}
