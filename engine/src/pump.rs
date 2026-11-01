//! pump.fun bonding curve: constants, event decoding, curve maths, instruction builders.
//! Layouts follow the on-chain Anchor IDL (engine/idl/pump.json) plus the two remaining
//! accounts observed on mainnet (bonding-curve-v2 PDA, buyback fee recipient).

use crate::solana::{ata, pk, AccountMeta, Instruction, Pubkey, SYSTEM_PROGRAM};
use anyhow::{bail, Result};
use sha2::{Digest, Sha256};

pub const PUMP: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
pub const PUMP_AMM: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
pub const PUMP_FEES: &str = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ";
pub const TOKEN_2022: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
pub const MAYHEM_AGENT: &str = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s";
pub const INIT_VSOL: u64 = 30_000_000_000;
pub const INIT_VTOK: u64 = 1_073_000_000_000_000;
pub const LAMPORTS: f64 = 1e9;

pub fn event_disc(name: &str) -> [u8; 8] {
    let h = Sha256::digest(format!("event:{name}").as_bytes());
    let mut d = [0u8; 8];
    d.copy_from_slice(&h[..8]);
    d
}

#[derive(Clone, Debug, Default)]
pub struct TradeEvent {
    pub mint: Pubkey,
    pub sol: u64,
    pub tok: u64,
    pub is_buy: bool,
    pub user: Pubkey,
    pub ts: i64,
    pub vsol: u64,
    pub vtok: u64,
    pub rsol: u64,
    pub rtok: u64,
    pub fee: u64,
    pub creator: Pubkey,
    pub creator_fee: u64,
    pub ix_name: String,
    pub mayhem: bool,
    pub quote_is_sol: bool,
}

#[derive(Clone, Debug, Default)]
pub struct CreateEvent {
    pub name: String,
    pub symbol: String,
    pub uri: String,
    pub mint: Pubkey,
    pub bonding_curve: Pubkey,
    pub user: Pubkey,
    pub creator: Pubkey,
    pub ts: i64,
    pub token_program: Pubkey,
    pub mayhem: bool,
    pub quote_is_sol: bool,
}

#[derive(Clone, Debug)]
pub enum PumpEvent {
    Trade(TradeEvent),
    Create(CreateEvent),
    Complete { mint: Pubkey },
}

struct Rd<'a> {
    b: &'a [u8],
    o: usize,
}
impl<'a> Rd<'a> {
    fn need(&self, n: usize) -> Result<()> {
        if self.o + n > self.b.len() {
            bail!("short event");
        }
        Ok(())
    }
    fn pk(&mut self) -> Result<Pubkey> {
        self.need(32)?;
        let mut a = [0u8; 32];
        a.copy_from_slice(&self.b[self.o..self.o + 32]);
        self.o += 32;
        Ok(Pubkey(a))
    }
    fn u64(&mut self) -> Result<u64> {
        self.need(8)?;
        let v = u64::from_le_bytes(self.b[self.o..self.o + 8].try_into().unwrap());
        self.o += 8;
        Ok(v)
    }
    fn i64(&mut self) -> Result<i64> {
        Ok(self.u64()? as i64)
    }
    fn u8(&mut self) -> Result<u8> {
        self.need(1)?;
        let v = self.b[self.o];
        self.o += 1;
        Ok(v)
    }
    fn u32(&mut self) -> Result<u32> {
        self.need(4)?;
        let v = u32::from_le_bytes(self.b[self.o..self.o + 4].try_into().unwrap());
        self.o += 4;
        Ok(v)
    }
    fn string(&mut self) -> Result<String> {
        let n = self.u32()? as usize;
        self.need(n)?;
        let s = String::from_utf8_lossy(&self.b[self.o..self.o + n]).to_string();
        self.o += n;
        Ok(s)
    }
}

pub struct Decoder {
    trade: [u8; 8],
    create: [u8; 8],
    complete: [u8; 8],
}

impl Default for Decoder {
    fn default() -> Self {
        Decoder { trade: event_disc("TradeEvent"), create: event_disc("CreateEvent"), complete: event_disc("CompleteEvent") }
    }
}

impl Decoder {
    pub fn decode(&self, data: &[u8]) -> Option<PumpEvent> {
        if data.len() < 8 {
            return None;
        }
        let d = &data[..8];
        let mut r = Rd { b: data, o: 8 };
        if d == self.trade {
            let mut e = TradeEvent {
                mint: r.pk().ok()?,
                sol: r.u64().ok()?,
                tok: r.u64().ok()?,
                is_buy: r.u8().ok()? == 1,
                user: r.pk().ok()?,
                ts: r.i64().ok()?,
                vsol: r.u64().ok()?,
                vtok: r.u64().ok()?,
                rsol: r.u64().ok()?,
                rtok: r.u64().ok()?,
                ..Default::default()
            };
            let _fee_rcpt = r.pk().ok()?;
            let _fee_bps = r.u64().ok()?;
            e.fee = r.u64().ok()?;
            e.creator = r.pk().ok()?;
            let _cbps = r.u64().ok()?;
            e.creator_fee = r.u64().ok()?;
            e.quote_is_sol = true;
            // optional tail (newer program versions)
            let tail = (|| -> Result<(String, bool, bool)> {
                r.u8()?;
                r.u64()?;
                r.u64()?;
                r.u64()?;
                r.i64()?;
                let ix = r.string()?;
                let mayhem = r.u8()? == 1;
                for _ in 0..4 {
                    r.u64()?;
                }
                let nsh = r.u32()? as usize;
                r.need(nsh * 34)?;
                r.o += nsh * 34;
                let q = r.pk()?;
                Ok((ix, mayhem, q == Pubkey::default() || q.to_b58() == SYSTEM_PROGRAM))
            })();
            if let Ok((ix, m, q)) = tail {
                e.ix_name = ix;
                e.mayhem = m;
                e.quote_is_sol = q;
            }
            return Some(PumpEvent::Trade(e));
        }
        if d == self.create {
            let mut e = CreateEvent {
                name: r.string().ok()?,
                symbol: r.string().ok()?,
                uri: r.string().ok()?,
                mint: r.pk().ok()?,
                bonding_curve: r.pk().ok()?,
                user: r.pk().ok()?,
                creator: r.pk().ok()?,
                ts: r.i64().ok()?,
                quote_is_sol: true,
                ..Default::default()
            };
            for _ in 0..4 {
                r.u64().ok()?;
            }
            if let Ok(tp) = r.pk() {
                e.token_program = tp;
                e.mayhem = r.u8().map(|x| x == 1).unwrap_or(false);
                let _cashback = r.u8();
                if let Ok(q) = r.pk() {
                    e.quote_is_sol = q == Pubkey::default() || q.to_b58() == SYSTEM_PROGRAM;
                }
            } else {
                e.token_program = pk("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
            }
            return Some(PumpEvent::Create(e));
        }
        if d == self.complete {
            let _user = r.pk().ok()?;
            return Some(PumpEvent::Complete { mint: r.pk().ok()? });
        }
        None
    }
}

/// Constant-product quotes on virtual reserves (lamports / raw token units).
pub fn buy_quote(vsol: f64, vtok: f64, sol_gross: f64, fee_rate: f64) -> f64 {
    let net = sol_gross / (1.0 + fee_rate);
    let k = vsol * vtok;
    vtok - k / (vsol + net * LAMPORTS)
}
pub fn sell_quote(vsol: f64, vtok: f64, tokens: f64, fee_rate: f64) -> f64 {
    let k = vsol * vtok;
    (vsol - k / (vtok + tokens)) / LAMPORTS * (1.0 - fee_rate)
}
pub fn mcap_sol(vsol: u64, vtok: u64) -> f64 {
    vsol as f64 / vtok as f64 * 1e15 / LAMPORTS
}

/// Accounts read once at startup from the Global account.
#[derive(Clone, Debug)]
pub struct GlobalCfg {
    pub fee_recipients: Vec<Pubkey>,
    pub reserved_fee_recipients: Vec<Pubkey>,
    pub buyback_recipients: Vec<Pubkey>,
}

impl GlobalCfg {
    /// Parse the Global account data (layout from the IDL `Global` struct).
    pub fn parse(d: &[u8]) -> Result<Self> {
        let mut r = Rd { b: d, o: 8 };
        r.u8()?; // initialized
        r.pk()?; // authority
        let main_fee = r.pk()?;
        for _ in 0..5 {
            r.u64()?;
        }
        r.pk()?; // withdraw_authority
        r.u8()?; // enable_migrate
        r.u64()?;
        r.u64()?;
        let mut fee_recipients = vec![main_fee];
        for _ in 0..7 {
            fee_recipients.push(r.pk()?);
        }
        r.pk()?;
        r.pk()?;
        r.u8()?;
        r.pk()?;
        let reserved_main = r.pk()?;
        r.u8()?; // mayhem_mode_enabled
        let mut reserved = vec![reserved_main];
        for _ in 0..7 {
            reserved.push(r.pk()?);
        }
        r.u8()?; // is_cashback_enabled
        let mut bb = vec![];
        for _ in 0..8 {
            bb.push(r.pk()?);
        }
        let nz = |v: Vec<Pubkey>| v.into_iter().filter(|p| *p != Pubkey::default()).collect::<Vec<_>>();
        Ok(GlobalCfg { fee_recipients: nz(fee_recipients), reserved_fee_recipients: nz(reserved), buyback_recipients: nz(bb) })
    }
}

pub struct PumpAccounts {
    pub program: Pubkey,
    pub global: Pubkey,
    pub event_authority: Pubkey,
    pub global_volume_accumulator: Pubkey,
    pub fee_config: Pubkey,
    pub fee_program: Pubkey,
}

impl Default for PumpAccounts {
    fn default() -> Self {
        let program = pk(PUMP);
        let fee_program = pk(PUMP_FEES);
        PumpAccounts {
            program,
            global: Pubkey::find_program_address(&[b"global"], &program).0,
            event_authority: Pubkey::find_program_address(&[b"__event_authority"], &program).0,
            global_volume_accumulator: Pubkey::find_program_address(&[b"global_volume_accumulator"], &program).0,
            fee_config: Pubkey::find_program_address(&[b"fee_config", &program.0], &fee_program).0,
            fee_program,
        }
    }
}

pub struct CoinKeys {
    pub mint: Pubkey,
    pub creator: Pubkey,
    pub token_program: Pubkey,
    pub mayhem: bool,
}

impl PumpAccounts {
    fn common(&self, c: &CoinKeys, user: &Pubkey) -> (Pubkey, Pubkey, Pubkey, Pubkey, Pubkey) {
        let bc = Pubkey::find_program_address(&[b"bonding-curve", &c.mint.0], &self.program).0;
        let abc = ata(&bc, &c.token_program, &c.mint);
        let au = ata(user, &c.token_program, &c.mint);
        let vault = Pubkey::find_program_address(&[b"creator-vault", &c.creator.0], &self.program).0;
        let bc2 = Pubkey::find_program_address(&[b"bonding-curve-v2", &c.mint.0], &self.program).0;
        (bc, abc, au, vault, bc2)
    }

    /// buy_exact_sol_in(spendable_sol_in, min_tokens_out, track_volume=Some(true))
    pub fn ix_buy_exact_sol_in(&self, g: &GlobalCfg, c: &CoinKeys, user: &Pubkey, spend: u64, min_out: u64, pick: usize) -> Instruction {
        let (bc, abc, au, vault, bc2) = self.common(c, user);
        let fees = if c.mayhem && !g.reserved_fee_recipients.is_empty() { &g.reserved_fee_recipients } else { &g.fee_recipients };
        let fee_rcpt = fees[pick % fees.len()];
        let bb = g.buyback_recipients[pick % g.buyback_recipients.len()];
        let uva = Pubkey::find_program_address(&[b"user_volume_accumulator", &user.0], &self.program).0;
        let mut data = vec![56, 252, 116, 8, 158, 223, 205, 95];
        data.extend_from_slice(&spend.to_le_bytes());
        data.extend_from_slice(&min_out.to_le_bytes());
        data.push(1);
        Instruction {
            program_id: self.program,
            accounts: vec![
                AccountMeta::r(self.global),
                AccountMeta::w(fee_rcpt),
                AccountMeta::r(c.mint),
                AccountMeta::w(bc),
                AccountMeta::w(abc),
                AccountMeta::w(au),
                AccountMeta::ws(*user),
                AccountMeta::r(pk(SYSTEM_PROGRAM)),
                AccountMeta::r(c.token_program),
                AccountMeta::w(vault),
                AccountMeta::r(self.event_authority),
                AccountMeta::r(self.program),
                AccountMeta::r(self.global_volume_accumulator),
                AccountMeta::w(uva),
                AccountMeta::r(self.fee_config),
                AccountMeta::r(self.fee_program),
                AccountMeta::w(bc2),
                AccountMeta::w(bb),
            ],
            data,
        }
    }

    /// sell(amount, min_sol_output)
    pub fn ix_sell(&self, g: &GlobalCfg, c: &CoinKeys, user: &Pubkey, amount: u64, min_sol: u64, pick: usize) -> Instruction {
        let (bc, abc, au, vault, bc2) = self.common(c, user);
        let fees = if c.mayhem && !g.reserved_fee_recipients.is_empty() { &g.reserved_fee_recipients } else { &g.fee_recipients };
        let fee_rcpt = fees[pick % fees.len()];
        let bb = g.buyback_recipients[pick % g.buyback_recipients.len()];
        let mut data = vec![51, 230, 133, 164, 1, 127, 131, 173];
        data.extend_from_slice(&amount.to_le_bytes());
        data.extend_from_slice(&min_sol.to_le_bytes());
        Instruction {
            program_id: self.program,
            accounts: vec![
                AccountMeta::r(self.global),
                AccountMeta::w(fee_rcpt),
                AccountMeta::r(c.mint),
                AccountMeta::w(bc),
                AccountMeta::w(abc),
                AccountMeta::w(au),
                AccountMeta::ws(*user),
                AccountMeta::r(pk(SYSTEM_PROGRAM)),
                AccountMeta::w(vault),
                AccountMeta::r(c.token_program),
                AccountMeta::r(self.event_authority),
                AccountMeta::r(self.program),
                AccountMeta::r(self.fee_config),
                AccountMeta::r(self.fee_program),
                AccountMeta::w(bc2),
                AccountMeta::w(bb),
            ],
            data,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    #[test]
    fn decode_trade_event_sample() {
        let s = "vdt/007mYe6tR0ulR9K+N6g88Cnls9JC74TcuIxGBDuo4Yy5scClX6OkmhQAAAAADEJOLbIEAAABtj1PCO8FhbOH991TvDvN6lOFHHR3fZP3YNaZfWY0GNpKuedqAAAAAMlvWNkKAAAAm77NY050AgDJwzTdAwAAAJsmuxe9dQEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABfAAAAAAAAAO8bMgAAAAAA/Mvol4/ITQMbt/xbsH2tDWlQdIzAaFPWjJZ9rJXCh/4eAAAAAAAAAO7SDwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEgAAAGJ1eV9leGFjdF9xdW90ZV9pbgAAAAAAAAAAAAAAAAAAAAAAiBMAAAAAAAD3DRkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKOkmhQAAAAAyW9Y2QoAAADJwzTdAwAAAAAAAAAAAAAAAAAAAAAAAACOhEwCAAAAAAAAAAAAAAAA";
        let b = base64::engine::general_purpose::STANDARD.decode(s).unwrap();
        match Decoder::default().decode(&b) {
            Some(PumpEvent::Trade(t)) => {
                assert_eq!(t.mint.to_b58(), "CfQayeoeGFokdc6zU56vzDMoXybMz9SudEyM8fkkpump");
                assert_eq!(t.sol, 345679011);
                assert!(t.is_buy);
                assert_eq!(t.vsol, 46596124617);
                assert_eq!(t.fee, 3283951);
                assert_eq!(t.creator_fee, 1037038);
                assert_eq!(t.ix_name, "buy_exact_quote_in");
                assert!(t.quote_is_sol);
                assert!(!t.mayhem);
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    #[test]
    fn pump_static_accounts_match_mainnet() {
        let a = PumpAccounts::default();
        assert_eq!(a.event_authority.to_b58(), "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1");
        assert_eq!(a.global_volume_accumulator.to_b58(), "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y");
        assert_eq!(a.fee_config.to_b58(), "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt");
    }
    #[test]
    fn curve_roundtrip_loses_two_fees() {
        let t = buy_quote(INIT_VSOL as f64, INIT_VTOK as f64, 0.5, 0.0125);
        let vs = INIT_VSOL as f64 + 0.5 / 1.0125 * LAMPORTS;
        let vt = INIT_VTOK as f64 - t;
        let back = sell_quote(vs, vt, t, 0.0125);
        assert!((back / 0.5 - (1.0 - 0.0125) / 1.0125).abs() < 1e-9);
    }
}
