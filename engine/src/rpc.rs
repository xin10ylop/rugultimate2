//! JSON-RPC client (getSlot, getBlock, getLatestBlockhash, getAccountInfo, simulate/send).

use anyhow::{anyhow, Result};
use base64::Engine;
use serde_json::{json, Value};
use std::time::Duration;

#[derive(Clone)]
pub struct Rpc {
    http: reqwest::Client,
    pub url: String,
}

impl Rpc {
    pub fn new(url: &str) -> Self {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .gzip(true)
            .pool_max_idle_per_host(16)
            .build()
            .expect("http client");
        Rpc { http, url: url.to_string() }
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
        let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
        let r = self.http.post(&self.url).json(&body).send().await?;
        let status = r.status();
        let v: Value = r.json().await?;
        if let Some(e) = v.get("error") {
            return Err(anyhow!("rpc {method} {status}: {e}"));
        }
        Ok(v["result"].clone())
    }

    pub async fn slot(&self) -> Result<u64> {
        Ok(self.call("getSlot", json!([{"commitment": "confirmed"}])).await?.as_u64().unwrap_or(0))
    }

    /// Full block in "json" encoding (instructions with base58 data, logs in meta).
    pub async fn block(&self, slot: u64) -> Result<Option<Value>> {
        match self
            .call(
                "getBlock",
                json!([slot, {"encoding": "json", "transactionDetails": "full",
                              "maxSupportedTransactionVersion": 1, "rewards": false, "commitment": "confirmed"}]),
            )
            .await
        {
            Ok(v) => Ok(if v.is_null() { None } else { Some(v) }),
            Err(e) => {
                let s = e.to_string();
                if s.contains("-32007") || s.contains("-32009") {
                    Ok(None) // skipped slot
                } else {
                    Err(e)
                }
            }
        }
    }

    pub async fn latest_blockhash(&self) -> Result<[u8; 32]> {
        let v = self.call("getLatestBlockhash", json!([{"commitment": "confirmed"}])).await?;
        let s = v["value"]["blockhash"].as_str().ok_or_else(|| anyhow!("no blockhash"))?;
        let b = bs58::decode(s).into_vec()?;
        Ok(b.try_into().map_err(|_| anyhow!("bad blockhash"))?)
    }

    pub async fn account_data(&self, addr: &str) -> Result<Vec<u8>> {
        let v = self.call("getAccountInfo", json!([addr, {"encoding": "base64"}])).await?;
        let s = v["value"]["data"][0].as_str().ok_or_else(|| anyhow!("no account {addr}"))?;
        Ok(base64::engine::general_purpose::STANDARD.decode(s)?)
    }

    pub async fn token_balance(&self, ata: &str) -> Result<u64> {
        let v = self.call("getTokenAccountBalance", json!([ata, {"commitment": "processed"}])).await?;
        Ok(v["value"]["amount"].as_str().unwrap_or("0").parse().unwrap_or(0))
    }

    /// simulateTransaction without signature verification; returns (err, logs).
    pub async fn simulate(&self, tx: &[u8]) -> Result<(Value, Vec<String>)> {
        let b64 = base64::engine::general_purpose::STANDARD.encode(tx);
        let v = self
            .call("simulateTransaction", json!([b64, {"encoding": "base64", "sigVerify": false,
                                                     "replaceRecentBlockhash": true, "commitment": "processed"}]))
            .await?;
        let logs = v["value"]["logs"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
        Ok((v["value"]["err"].clone(), logs))
    }

    /// sendTransaction (works for standard RPCs and Nozomi/Jito-style HTTP senders).
    pub async fn send(&self, tx: &[u8]) -> Result<String> {
        let b64 = base64::engine::general_purpose::STANDARD.encode(tx);
        let v = self
            .call("sendTransaction", json!([b64, {"encoding": "base64", "skipPreflight": true, "maxRetries": 0}]))
            .await?;
        Ok(v.as_str().unwrap_or_default().to_string())
    }
}
