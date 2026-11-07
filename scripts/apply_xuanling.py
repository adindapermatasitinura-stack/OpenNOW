#!/usr/bin/env python3
"""Install the XuanLing login provider into OpenNOW's Rust core.

Run from anywhere inside the repo (the GitHub Actions workflow does this).
Idempotent: parts that are already applied are skipped.
Nothing is written unless every anchor was found; on a mismatch the script
prints the surrounding code and exits with an error.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GFN = ROOT / "native/opennow-core/src/gfn.rs"
CLOUDMATCH = ROOT / "native/opennow-core/src/cloudmatch.rs"

# Taken from the XuanLing Android APK (classes.dex).
IDP_ID = "f1ea2881fa3bf4783538e526fa3d1f41"
HOST = "oauth.xuanling.my.id"

CONSTS_BLOCK = """
// XUANLING-PATCH: provider
pub const XUANLING_IDP_ID: &str = "@IDP@";
const XUANLING_BASE: &str = "https://@HOST@";

impl LoginProvider {
    #[allow(dead_code)]
    pub fn xuanling() -> Self {
        Self {
            idp_id: XUANLING_IDP_ID.to_owned(),
            code: "XuanLing".to_owned(),
            display_name: "XuanLing".to_owned(),
            streaming_service_url: format!("{XUANLING_BASE}/cloudmatchbeta/default/"),
            priority: 100,
        }
    }
}
"""

ENDPOINTS_BLOCK = """
// XUANLING-PATCH: endpoints
impl Endpoints {
    #[allow(dead_code)]
    pub fn xuanling() -> Self {
        let base = XUANLING_BASE;
        Self {
            device_authorize: format!("{base}/device/authorize"),
            token: format!("{base}/token"),
            client_token: format!("{base}/client_token"),
            userinfo: format!("{base}/userinfo"),
            revoke: format!("{base}/assets/v2/Tokens?level=client"),
            graphql: format!("{base}/graphql/games"),
            subscription: format!("{base}/v4/subscriptions"),
            account_linking: format!("{base}/als/v1"),
            ..Self::default()
        }
    }
}
"""

CLOUDMATCH_GUARD = """
    // XUANLING-PATCH: allow the XuanLing streaming host
    if let Ok(xuanling_url) = url::Url::parse(@PARAM@.trim()) {
        if xuanling_url.scheme() == "https"
            && xuanling_url.host_str() == Some("@HOST@")
        {
            return Ok(xuanling_url);
        }
    }
"""

PROVIDER_LIST_BLOCK = """{b}// XUANLING-PATCH: provider list
{b}#[cfg(not(test))]
{b}{{
{b}    if !providers
{b}        .iter()
{b}        .any(|provider| provider.idp_id == XUANLING_IDP_ID)
{b}    {{
{b}        providers.push(LoginProvider::xuanling());
{b}    }}
{b}}}"""


def die(msg):
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def fill(template):
    return template.replace("@IDP@", IDP_ID).replace("@HOST@", HOST)


def context(lines, i, before=8, after=10):
    a, b = max(0, i - before), min(len(lines), i + after)
    return "\n".join(f"{n + 1:6}| {lines[n]}" for n in range(a, b))


def patch_gfn(text, log):
    # 1. constants, LoginProvider::xuanling(), Endpoints::xuanling()
    m = re.search(r"impl Default for Endpoints \{.*?\n\}\n", text, re.S)
    if not m:
        die("gfn.rs: could not find `impl Default for Endpoints { ... }`")
    add = ""
    if "XUANLING_IDP_ID" in text:
        log.append("gfn.rs: provider constants already present")
    else:
        add += fill(CONSTS_BLOCK)
        log.append("gfn.rs: added provider constants + LoginProvider::xuanling()")
    if 'format!("{base}/device/authorize")' in text:
        log.append("gfn.rs: Endpoints::xuanling() already present")
    else:
        add += fill(ENDPOINTS_BLOCK)
        log.append("gfn.rs: added Endpoints::xuanling()")
    text = text[: m.end()] + add + text[m.end():]
    if "const XUANLING_BASE" not in text:
        die("gfn.rs: XUANLING_BASE constant is missing after patching")

    # 2. the service is built once with one set of endpoints
    old = "Ok(Self::with_client(client, Endpoints::default(), data_dir))"
    new = "Ok(Self::with_client(client, Endpoints::xuanling(), data_dir))"
    if new in text:
        log.append("gfn.rs: service already uses Endpoints::xuanling()")
    elif text.count(old) == 1:
        text = text.replace(old, new)
        log.append("gfn.rs: service now uses Endpoints::xuanling()")
    else:
        die(f"gfn.rs: expected exactly one `{old}`, found {text.count(old)}")

    # 3. add XuanLing to the provider list
    if "XUANLING-PATCH: provider list" in text:
        log.append("gfn.rs: provider list already patched")
        return text
    push = "providers.push(LoginProvider::default_nvidia());"
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.strip() == push]
    if len(hits) != 1:
        die(f"gfn.rs: expected one `{push}`, found {len(hits)}")
    i = hits[0]
    indent = len(lines[i]) - len(lines[i].lstrip())
    if indent < 4:
        die("gfn.rs: unexpected indentation around the provider push\n" + context(lines, i))
    base = " " * (indent - 4)
    close = next(
        (k for k in range(i + 1, min(i + 8, len(lines))) if lines[k] == base + "}"),
        None,
    )
    if close is None:
        die("gfn.rs: could not find the end of the block holding the provider push\n" + context(lines, i))
    block = PROVIDER_LIST_BLOCK.format(b=base).split("\n")
    lines[close + 1:close + 1] = block
    log.append("gfn.rs: provider list patched. Context:\n" + context(lines, i, 6, 20))
    return "\n".join(lines)


def patch_cloudmatch(text, log):
    if "XUANLING-PATCH" in text:
        log.append("cloudmatch.rs: already patched")
        return text
    m = re.search(
        r"(?:pub(?:\([a-z]+\))?\s+)?fn\s+trusted_cloudmatch_base\s*"
        r"\(\s*(\w+)\s*:\s*&str\s*\)\s*->\s*([^{]+?)\s*\{",
        text,
    )
    if not m:
        idx = text.find("trusted_cloudmatch_base")
        lines = text.split("\n")
        where = text[:idx].count("\n") if idx >= 0 else 0
        die("cloudmatch.rs: could not match the signature of trusted_cloudmatch_base\n"
            + context(lines, where, 2, 8))
    param, ret = m.group(1), m.group(2)
    if "Result" not in ret or "Url" not in ret:
        die(f"cloudmatch.rs: unexpected return type `{ret}`; send me this signature")
    guard = fill(CLOUDMATCH_GUARD).replace("@PARAM@", param)
    text = text[: m.end()] + guard + text[m.end():]
    log.append(f"cloudmatch.rs: guard added (param `{param}`, returns `{ret}`)")
    return text


def main():
    for path in (GFN, CLOUDMATCH):
        if not path.exists():
            die(f"missing file: {path}")
    log = []
    new_gfn = patch_gfn(GFN.read_text(encoding="utf-8"), log)
    new_cm = patch_cloudmatch(CLOUDMATCH.read_text(encoding="utf-8"), log)
    GFN.write_text(new_gfn, encoding="utf-8")
    CLOUDMATCH.write_text(new_cm, encoding="utf-8")
    print("\n".join(log))
    print("\nDone.")


if __name__ == "__main__":
    main()