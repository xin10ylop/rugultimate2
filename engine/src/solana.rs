//! Minimal Solana primitives: pubkeys, PDAs, keypairs, legacy transactions.
//! Kept dependency-light on purpose (no solana-sdk version churn).

use anyhow::{anyhow, bail, Result};
use curve25519_dalek::edwards::CompressedEdwardsY;
use ed25519_dalek::{Signer, SigningKey};
use sha2::{Digest, Sha256};
use std::fmt;

#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct Pubkey(pub [u8; 32]);

impl Pubkey {
    pub fn from_str(s: &str) -> Result<Self> {
        let v = bs58::decode(s).into_vec()?;
        if v.len() != 32 {
            bail!("bad pubkey length {} for {s}", v.len());
        }
        let mut a = [0u8; 32];
        a.copy_from_slice(&v);
        Ok(Pubkey(a))
    }
    pub fn to_b58(&self) -> String {
        bs58::encode(self.0).into_string()
    }
    pub fn is_on_curve(&self) -> bool {
        CompressedEdwardsY(self.0).decompress().is_some()
    }
    pub fn create_program_address(seeds: &[&[u8]], program: &Pubkey) -> Option<Pubkey> {
        let mut h = Sha256::new();
        for s in seeds {
            h.update(s);
        }
        h.update(program.0);
        h.update(b"ProgramDerivedAddress");
        let out: [u8; 32] = h.finalize().into();
        let pk = Pubkey(out);
        if pk.is_on_curve() {
            None
        } else {
            Some(pk)
        }
    }
    pub fn find_program_address(seeds: &[&[u8]], program: &Pubkey) -> (Pubkey, u8) {
        for bump in (0..=255u8).rev() {
            let b = [bump];
            let mut s: Vec<&[u8]> = seeds.to_vec();
            s.push(&b);
            if let Some(pk) = Self::create_program_address(&s, program) {
                return (pk, bump);
            }
        }
        panic!("no viable bump");
    }
}

impl fmt::Display for Pubkey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_b58())
    }
}
impl fmt::Debug for Pubkey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.to_b58())
    }
}

pub fn pk(s: &str) -> Pubkey {
    Pubkey::from_str(s).expect("static pubkey")
}

pub struct Keypair {
    sk: SigningKey,
}

impl Keypair {
    /// Accepts the usual 64-byte JSON array format (solana-keygen) or a base58 secret.
    pub fn from_file(path: &str) -> Result<Self> {
        let txt = std::fs::read_to_string(path)?;
        let t = txt.trim();
        let bytes: Vec<u8> = if t.starts_with('[') {
            serde_json::from_str(t)?
        } else {
            bs58::decode(t).into_vec()?
        };
        if bytes.len() < 32 {
            bail!("keypair file too short");
        }
        let mut s = [0u8; 32];
        s.copy_from_slice(&bytes[..32]);
        Ok(Keypair { sk: SigningKey::from_bytes(&s) })
    }
    pub fn generate() -> Self {
        Keypair { sk: SigningKey::generate(&mut rand::rngs::OsRng) }
    }
    pub fn pubkey(&self) -> Pubkey {
        Pubkey(self.sk.verifying_key().to_bytes())
    }
    pub fn sign(&self, msg: &[u8]) -> [u8; 64] {
        self.sk.sign(msg).to_bytes()
    }
}

#[derive(Clone, Debug)]
pub struct AccountMeta {
    pub pubkey: Pubkey,
    pub is_signer: bool,
    pub is_writable: bool,
}
impl AccountMeta {
    pub fn w(pubkey: Pubkey) -> Self {
        AccountMeta { pubkey, is_signer: false, is_writable: true }
    }
    pub fn r(pubkey: Pubkey) -> Self {
        AccountMeta { pubkey, is_signer: false, is_writable: false }
    }
    pub fn ws(pubkey: Pubkey) -> Self {
        AccountMeta { pubkey, is_signer: true, is_writable: true }
    }
}

#[derive(Clone, Debug)]
pub struct Instruction {
    pub program_id: Pubkey,
    pub accounts: Vec<AccountMeta>,
    pub data: Vec<u8>,
}

pub const SYSTEM_PROGRAM: &str = "11111111111111111111111111111111";
pub const COMPUTE_BUDGET: &str = "ComputeBudget111111111111111111111111111111";
pub const ATA_PROGRAM: &str = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";

pub fn ix_cu_limit(units: u32) -> Instruction {
    let mut d = vec![2u8];
    d.extend_from_slice(&units.to_le_bytes());
    Instruction { program_id: pk(COMPUTE_BUDGET), accounts: vec![], data: d }
}
pub fn ix_cu_price(micro_lamports: u64) -> Instruction {
    let mut d = vec![3u8];
    d.extend_from_slice(&micro_lamports.to_le_bytes());
    Instruction { program_id: pk(COMPUTE_BUDGET), accounts: vec![], data: d }
}
pub fn ix_transfer(from: Pubkey, to: Pubkey, lamports: u64) -> Instruction {
    let mut d = 2u32.to_le_bytes().to_vec();
    d.extend_from_slice(&lamports.to_le_bytes());
    Instruction { program_id: pk(SYSTEM_PROGRAM), accounts: vec![AccountMeta::ws(from), AccountMeta::w(to)], data: d }
}
/// Associated token account address for (owner, token_program, mint).
pub fn ata(owner: &Pubkey, token_program: &Pubkey, mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[&owner.0, &token_program.0, &mint.0], &pk(ATA_PROGRAM)).0
}
/// CreateIdempotent associated token account instruction.
pub fn ix_create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Instruction {
    let a = ata(&owner, &token_program, &mint);
    Instruction {
        program_id: pk(ATA_PROGRAM),
        accounts: vec![
            AccountMeta::ws(payer),
            AccountMeta::w(a),
            AccountMeta::r(owner),
            AccountMeta::r(mint),
            AccountMeta::r(pk(SYSTEM_PROGRAM)),
            AccountMeta::r(token_program),
        ],
        data: vec![1u8],
    }
}

fn shortvec(n: usize, out: &mut Vec<u8>) {
    let mut v = n;
    loop {
        let mut b = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            b |= 0x80;
        }
        out.push(b);
        if v == 0 {
            break;
        }
    }
}

/// Compile and sign a legacy transaction. Returns serialized wire bytes.
pub fn build_legacy_tx(payer: &Keypair, ixs: &[Instruction], recent_blockhash: &[u8; 32]) -> Result<Vec<u8>> {
    build_tx_inner(payer.pubkey(), Some(payer), ixs, recent_blockhash)
}

/// Unsigned (zero-signature) transaction for simulateTransaction with sigVerify=false.
pub fn build_unsigned_tx(payer: Pubkey, ixs: &[Instruction], recent_blockhash: &[u8; 32]) -> Result<Vec<u8>> {
    build_tx_inner(payer, None, ixs, recent_blockhash)
}

fn build_tx_inner(payer_pk: Pubkey, signer: Option<&Keypair>, ixs: &[Instruction], recent_blockhash: &[u8; 32]) -> Result<Vec<u8>> {
    // ordered unique account list: payer first, then signer-writable, signer-ro, writable, ro
    let mut metas: Vec<AccountMeta> = vec![AccountMeta::ws(payer_pk)];
    let mut push = |m: &AccountMeta, metas: &mut Vec<AccountMeta>| {
        if let Some(e) = metas.iter_mut().find(|x| x.pubkey == m.pubkey) {
            e.is_signer |= m.is_signer;
            e.is_writable |= m.is_writable;
        } else {
            metas.push(m.clone());
        }
    };
    for ix in ixs {
        for a in &ix.accounts {
            push(a, &mut metas);
        }
        push(&AccountMeta::r(ix.program_id), &mut metas);
    }
    let rank = |m: &AccountMeta| match (m.is_signer, m.is_writable) {
        (true, true) => 0,
        (true, false) => 1,
        (false, true) => 2,
        (false, false) => 3,
    };
    let head = metas.remove(0);
    metas.sort_by_key(|m| rank(m));
    metas.insert(0, head);
    let signers: Vec<&AccountMeta> = metas.iter().filter(|m| m.is_signer).collect();
    if signers.len() != 1 {
        return Err(anyhow!("only single-signer transactions supported"));
    }
    let n_ro_signed = metas.iter().filter(|m| m.is_signer && !m.is_writable).count();
    let n_ro_unsigned = metas.iter().filter(|m| !m.is_signer && !m.is_writable).count();
    let index = |p: &Pubkey| metas.iter().position(|m| &m.pubkey == p).unwrap() as u8;

    let mut msg = vec![1u8, n_ro_signed as u8, n_ro_unsigned as u8];
    shortvec(metas.len(), &mut msg);
    for m in &metas {
        msg.extend_from_slice(&m.pubkey.0);
    }
    msg.extend_from_slice(recent_blockhash);
    shortvec(ixs.len(), &mut msg);
    for ix in ixs {
        msg.push(index(&ix.program_id));
        shortvec(ix.accounts.len(), &mut msg);
        for a in &ix.accounts {
            msg.push(index(&a.pubkey));
        }
        shortvec(ix.data.len(), &mut msg);
        msg.extend_from_slice(&ix.data);
    }
    let sig = signer.map(|k| k.sign(&msg)).unwrap_or([0u8; 64]);
    let mut tx = vec![];
    shortvec(1, &mut tx);
    tx.extend_from_slice(&sig);
    tx.extend_from_slice(&msg);
    Ok(tx)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pda_matches_known_pump_global() {
        // global PDA of pump.fun (seed "global")
        let prog = pk("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
        let (g, _) = Pubkey::find_program_address(&[b"global"], &prog);
        assert_eq!(g.to_b58(), "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf");
    }
    #[test]
    fn bonding_curve_v2_pda() {
        let prog = pk("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
        let mint = pk("93k3Awd9QGhSvxFAsDVeDqJLJeCJ6cXAzJXSYtP6pump");
        let (a, _) = Pubkey::find_program_address(&[b"bonding-curve-v2", &mint.0], &prog);
        assert_eq!(a.to_b58(), "9A64RGdevMJQLsauM1tas4qE46wky6ozasgXFZ9vygFs");
    }
}
