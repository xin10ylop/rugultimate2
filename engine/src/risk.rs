//! Hard risk limits. Every entry must pass `allow`; every close reports to `on_close`.

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RiskCfg {
    pub size_sol: f64,
    pub max_open: usize,
    pub daily_loss_limit_sol: f64,
    pub key_loss_streak: u32,
    pub max_entries_per_key_per_hour: u32,
    pub kill_file: String,
}

impl Default for RiskCfg {
    fn default() -> Self {
        RiskCfg { size_sol: 0.05, max_open: 3, daily_loss_limit_sol: 0.3, key_loss_streak: 3,
                  max_entries_per_key_per_hour: 6, kill_file: "STOP".into() }
    }
}

pub struct Risk {
    pub cfg: RiskCfg,
    pub open: usize,
    day: i64,
    pub day_pnl: f64,
    streak: HashMap<String, u32>,
    paused: HashSet<String>,
    recent: HashMap<String, Vec<i64>>,
}

impl Risk {
    pub fn new(cfg: RiskCfg) -> Self {
        Risk { cfg, open: 0, day: 0, day_pnl: 0.0, streak: HashMap::new(), paused: HashSet::new(), recent: HashMap::new() }
    }

    pub fn allow(&mut self, key: &str, now_unix: i64) -> Result<(), String> {
        let d = now_unix / 86400;
        if d != self.day {
            self.day = d;
            self.day_pnl = 0.0;
        }
        if std::path::Path::new(&self.cfg.kill_file).exists() {
            return Err("kill file present".into());
        }
        if self.day_pnl <= -self.cfg.daily_loss_limit_sol {
            return Err(format!("daily loss limit hit ({:.3} SOL)", self.day_pnl));
        }
        if self.open >= self.cfg.max_open {
            return Err("max open positions".into());
        }
        if self.paused.contains(key) {
            return Err(format!("key paused after {} consecutive losses", self.cfg.key_loss_streak));
        }
        let r = self.recent.entry(key.to_string()).or_default();
        r.retain(|t| now_unix - t < 3600);
        if r.len() as u32 >= self.cfg.max_entries_per_key_per_hour {
            return Err("operator rate limit (possible farming)".into());
        }
        r.push(now_unix);
        self.open += 1;
        Ok(())
    }

    pub fn on_close(&mut self, key: &str, pnl_sol: f64) {
        self.open = self.open.saturating_sub(1);
        self.day_pnl += pnl_sol;
        let s = self.streak.entry(key.to_string()).or_default();
        if pnl_sol < 0.0 {
            *s += 1;
            if *s >= self.cfg.key_loss_streak {
                self.paused.insert(key.to_string());
            }
        } else {
            *s = 0;
        }
    }
}
