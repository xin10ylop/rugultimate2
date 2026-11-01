//! Turn a getBlock (json encoding) response into ordered engine events.
//! Mirrors research/collector/block_collector.py so research and live see identical data.

use crate::pump::{Decoder, PumpEvent, PUMP};
use crate::solana::{Pubkey, SYSTEM_PROGRAM};
use base64::Engine;
use serde_json::Value;

#[derive(Clone, Debug)]
pub struct Transfer {
    pub from: Pubkey,
    pub to: Pubkey,
    pub lamports: u64,
    pub to_pre_balance: u64,
}

#[derive(Clone, Debug)]
pub enum Ev {
    Pump(PumpEvent),
    Transfer(Transfer),
    /// A failed transaction that touched pump.fun (bot pressure); mints are best-effort.
    FailedPump { signer: Pubkey, mints: Vec<Pubkey> },
}

#[derive(Clone, Debug)]
pub struct Located {
    pub slot: u64,
    pub idx: u32,
    pub ev: Ev,
}

pub fn parse_block(slot: u64, blk: &Value, dec: &Decoder, min_xfer: u64) -> Vec<Located> {
    let mut out = vec![];
    let txs = match blk["transactions"].as_array() {
        Some(t) => t,
        None => return out,
    };
    let pump = PUMP.to_string();
    for (idx, tx) in txs.iter().enumerate() {
        let meta = &tx["meta"];
        let msg = &tx["transaction"]["message"];
        let mut keys: Vec<String> = msg["accountKeys"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
        for k in ["writable", "readonly"] {
            if let Some(a) = meta["loadedAddresses"][k].as_array() {
                keys.extend(a.iter().filter_map(|x| x.as_str().map(String::from)));
            }
        }
        let ok = meta["err"].is_null();
        let touches_pump = keys.iter().any(|k| *k == pump);
        if !ok {
            if touches_pump {
                let mints = keys.iter().filter(|k| k.ends_with("pump")).filter_map(|k| Pubkey::from_str(k).ok()).take(2).collect();
                if let Some(s) = keys.first().and_then(|k| Pubkey::from_str(k).ok()) {
                    out.push(Located { slot, idx: idx as u32, ev: Ev::FailedPump { signer: s, mints } });
                }
            }
            continue;
        }
        let pre: Vec<u64> = meta["preBalances"].as_array().map(|a| a.iter().map(|x| x.as_u64().unwrap_or(0)).collect()).unwrap_or_default();
        // system transfers (top-level + inner)
        let mut ixs: Vec<&Value> = msg["instructions"].as_array().map(|a| a.iter().collect()).unwrap_or_default();
        if let Some(inner) = meta["innerInstructions"].as_array() {
            for g in inner {
                if let Some(a) = g["instructions"].as_array() {
                    ixs.extend(a.iter());
                }
            }
        }
        for ix in ixs {
            let pidx = ix["programIdIndex"].as_u64().unwrap_or(u64::MAX) as usize;
            if keys.get(pidx).map(|s| s.as_str()) != Some(SYSTEM_PROGRAM) {
                continue;
            }
            let data = match ix["data"].as_str().and_then(|d| bs58::decode(d).into_vec().ok()) {
                Some(d) => d,
                None => continue,
            };
            if data.len() >= 12 && data[0..4] == [2, 0, 0, 0] {
                let lam = u64::from_le_bytes(data[4..12].try_into().unwrap());
                let acc: Vec<usize> = ix["accounts"].as_array().map(|a| a.iter().map(|x| x.as_u64().unwrap_or(0) as usize).collect()).unwrap_or_default();
                if lam >= min_xfer && acc.len() >= 2 {
                    if let (Some(f), Some(t)) = (keys.get(acc[0]), keys.get(acc[1])) {
                        if let (Ok(f), Ok(t)) = (Pubkey::from_str(f), Pubkey::from_str(t)) {
                            out.push(Located { slot, idx: idx as u32, ev: Ev::Transfer(Transfer { from: f, to: t, lamports: lam, to_pre_balance: *pre.get(acc[1]).unwrap_or(&0) }) });
                        }
                    }
                }
            }
        }
        if !touches_pump {
            continue;
        }
        // pump events from logs, attributed to the emitting program via the invoke stack
        let mut stack: Vec<&str> = vec![];
        if let Some(logs) = meta["logMessages"].as_array() {
            for l in logs.iter().filter_map(|x| x.as_str()) {
                if l.starts_with("Program ") && l.contains(" invoke [") {
                    stack.push(l.split(' ').nth(1).unwrap_or(""));
                } else if l.starts_with("Program ") && (l.ends_with(" success") || l.contains(" failed")) {
                    stack.pop();
                } else if let Some(b64) = l.strip_prefix("Program data: ") {
                    if stack.last() == Some(&PUMP) {
                        if let Ok(b) = base64::engine::general_purpose::STANDARD.decode(b64) {
                            if let Some(e) = dec.decode(&b) {
                                out.push(Located { slot, idx: idx as u32, ev: Ev::Pump(e) });
                            }
                        }
                    }
                }
            }
        }
    }
    out
}
