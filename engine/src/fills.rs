//! Never-first fill model and exit-policy grid (port of research/lib/sim.py + grid.py).
//! Used for (a) learning operator track records and (b) paper trading, so live paper results are
//! computed exactly like the backtest.

use crate::pump::{buy_quote, sell_quote};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug)]
pub struct Tick {
    pub slot: u64,
    pub vsol: f64,
    pub vtok: f64,
}
impl Tick {
    pub fn px(&self) -> f64 {
        self.vsol / self.vtok
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct Costs {
    pub buy_fixed: f64,
    pub sell_fixed: f64,
    pub fail_fixed: f64,
    pub max_slip: f64,
}
impl Default for Costs {
    fn default() -> Self {
        Costs { buy_fixed: 0.0015, sell_fixed: 0.0015, fail_fixed: 0.0006, max_slip: 0.20 }
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
pub struct Policy {
    pub tp: f64,
    pub sl: f64,
    pub max_slots: u64,
}

pub fn policy_grid() -> Vec<Policy> {
    let mut v = vec![];
    for tp in [0.10, 0.20, 0.30, 0.50, 0.75, 1.00, 1.50, 2.50] {
        for sl in [0.20, 0.35, 0.50, 0.95] {
            for t in [47u64, 140, 560, 2800] {
                v.push(Policy { tp, sl, max_slots: t });
            }
        }
    }
    v
}

#[derive(Clone, Copy, PartialEq)]
pub enum Landing {
    SameSlot(usize), // behind this many further trades of the coin in the trigger slot
    NextSlot(usize), // after the trigger slot, behind this many trades of the next slot
}

/// Row whose post-trade state our transaction executes on.
pub fn landing_row(t: &[Tick], j: usize, l: Landing) -> usize {
    let n = t.len();
    let s = t[j].slot;
    let mut end_same = j;
    while end_same + 1 < n && t[end_same + 1].slot == s {
        end_same += 1;
    }
    match l {
        Landing::SameSlot(m) => (j + m).min(end_same),
        Landing::NextSlot(m) => {
            let k = end_same;
            if k + 1 >= n || t[k + 1].slot > s + 1 {
                return k;
            }
            let ns = t[k + 1].slot;
            let mut r = k;
            let mut c = 0;
            while r + 1 < n && t[r + 1].slot == ns && c < m {
                r += 1;
                c += 1;
            }
            r
        }
    }
}

/// Returns per-policy net returns for an entry triggered at row j.
pub fn grid_one(t: &[Tick], j: usize, size: f64, fee: f64, co: &Costs, pol: &[Policy], le: Landing, lx: Landing) -> Vec<f64> {
    let n = t.len();
    let e = landing_row(t, j, le);
    if t[e].px() > t[j].px() * (1.0 + co.max_slip) {
        return vec![-co.fail_fixed / size; pol.len()];
    }
    let toks = buy_quote(t[e].vsol, t[e].vtok, size, fee);
    let refp = size * 1e9 / toks;
    let exit_ret = |r: usize| -> f64 {
        let xr = if r >= n - 1 { r } else { landing_row(t, r, lx) };
        (sell_quote(t[xr].vsol, t[xr].vtok, toks, fee) - size - co.buy_fixed - co.sell_fixed) / size
    };
    pol.iter()
        .map(|p| {
            let mut r = n - 1;
            for i in e + 1..n {
                let rel = t[i].px() / refp;
                if rel >= 1.0 + p.tp || rel <= 1.0 - p.sl || t[i].slot - t[e].slot >= p.max_slots {
                    r = i;
                    break;
                }
            }
            exit_ret(r)
        })
        .collect()
}

/// Average of same-slot and next-slot landings (the measured ~50/50 split).
pub fn grid_avg(t: &[Tick], j: usize, size: f64, fee: f64, co: &Costs, pol: &[Policy], m_same: usize, m_next: usize) -> Vec<f64> {
    let a = grid_one(t, j, size, fee, co, pol, Landing::SameSlot(m_same), Landing::SameSlot(m_same));
    let b = grid_one(t, j, size, fee, co, pol, Landing::NextSlot(m_next), Landing::NextSlot(m_next));
    a.iter().zip(b.iter()).map(|(x, y)| 0.5 * (x + y)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn flat_tape_loses_costs() {
        let t: Vec<Tick> = (0..50).map(|i| Tick { slot: i, vsol: 30e9, vtok: 1.073e15 }).collect();
        let pol = vec![Policy { tp: 0.5, sl: 0.5, max_slots: 10 }];
        let r = grid_avg(&t, 2, 0.25, 0.0125, &Costs::default(), &pol, 9, 1);
        // two fees + own impact twice + fixed costs, never positive
        assert!(r[0] < -0.03 && r[0] > -0.06, "{r:?}");
    }
}
