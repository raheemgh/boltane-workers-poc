#!/usr/bin/env python3
"""
install_probe.py - creates the throwaway probe Worker project in a NEW sibling folder, ../cf-probe

HOW (one step, from the cf-worker folder):
    cd /workspaces/boltane-workers-poc/files12/cf-worker
    python install_probe.py

It only creates files inside ../cf-probe (and refuses if that folder already exists). Nothing in cf-worker is touched.
Then:
    cd ../cf-probe
    uv run pywrangler deploy                     # same terminal where CLOUDFLARE_API_TOKEN is exported
    python probe_client.py --url https://boltane-probe.boltane.workers.dev
When the experiments are done, delete the probe:   npx wrangler delete --name boltane-probe
"""
import base64, hashlib, py_compile, sys, zlib
from pathlib import Path

FILES = {
 "wrangler.jsonc": {
  "sha256": "af37ac0c8c58304f183ce2c023f4990f936624e494cbc64b21e43b25db2edc7a",
  "data": "eNpVjrsOwjAMRWf6FVZm+lAHBKyoX8DAgFDltqaNSOMotVRFiH8n6dbV5957/M0OyuJM6gqqYyNoKXeeO1LHSGbUNpHF9+V2LVxIoCzhHkvQ8+xQdKeNlgADCoFMBDf2BI0dtSV4sP+QBzSecIgZcoYDDbBqmYqo2E20aSIJ66o+5dUlr8/bH/vQ2+C4xNRTuSAT23bdHIt6Zb/sD6VkRKA="
 },
 "pyproject.toml": {
  "sha256": "ca210985306ea19b3e3793f91e68d84c418c31ff0418f1016b0494ab14b6a5ee",
  "data": "eNpVj7FuwzAMRHd9BeHZNmIEXQo4RTMU6JYhRQcjg2IxsWqbVCU5qv++tFs0KEfy3vGucZ4/sI0nRXpEqCE78xA1YSGHM2bqhj5YpuWyKatykymDofXWxd/tsfOcdNIzrAS8s+/RP4JhDPCiQ3w+vMJet/3V80TmqEMfwOAgQOwQPAbHFDD/Adr4VSZt4xtFO0ASL7h4HuEwx44pB00GOk4wMF1hFA8Nd3kUb/ATPWXK4+dkxbtwK7gE3dXbstou+R2SQWqt/KuhUSCTXSSpdnZXS8vqQXrm6qRU8yeeiyW/Cyfhb3csrW2XNwL820iOaEcsgulXr2+ivXfQ"
 },
 ".gitignore": {
  "sha256": "64b31510c00003f0b268bb22c4f1e9fb6eea09ea2b11758698514dbffb5ed68b",
  "data": "eNorqCzJyM+Lz81PKc1JLdbn0itLzSvTLc8vyk4tgnH1ueLjCyqTE5MzUuPj9bkA9DwSTA=="
 },
 "src/probe.py": {
  "sha256": "170dc51864c647eac8ae659bffdae025c2689773040abd94167cf5288c91f1c1",
  "data": "eNqtVm1v2zYQ/q5fcVA/VEIdxVm7FxhLt6zzhmJr2jUuNiAIXFo6WVxkUiWpOF7g/747ipIdpy0KbPkQS+S9PvfcneI4jmaV0WuxFhtojF4g/KnNNRpILpxYInyTZnCuwWJu0NkRKA2FcGIhLPKLq6RawqJ14OQKjc2i6KUDoeyaXsBVBhE+tGid1MqCWGiWrBAuzl5N4Rdh3dmbl/AE1t6nzYRdSvLl2sZLvdAGYaqWUiG0Fu0kigDevL6YwfFiefw9BaVVYZ8D2LzCoq3Rwns29R7W0lWD/Z9Efr00ulXFTNhrSgJvRe7qDdTyGmGNi0rr66zZQKGRMoCP/v0Rsph4KR+eQdtQWgi5XiEsyAsIB1rlBI029EAuROkITJZ25BsqYaGUSlqK94chmXX72WQIRt0uK2+lq85jSzlg3nJA5F05vHUjyN1tthbSvVNO1kn6hZmQFhmTlstWgMhzbOgR3mxcRcY56tEXpfwJb2x0UPcYkPO6DigEfL4GC8fwbOx/vuWf5EWt26KsBTGg0Hm7QuWIQPCUL2u5ki5l/H6dzgCOpSr1zuO6knkF0upaOAxcxGLkI+nuOAxLtbjBoRiQ+NiI83D0vA+OOU0wRtQKZKmWN5SGVLDSXB5Y1nohaqKT1YCCzHauIBfGSJIUaghCFhOKfSV8Z0nbUcOZ1jrkmFB5dKxYYdSruIqAtU4YFhmwI90OPj7Rqk+PmjCLYmpmuWq0ofLZjcql7l85j/65bWURRaXRKyipP0QjIdw8aJPQP51waNFeuOPhVDmzabRUxD7u3Sh6RNOiGwqG8CY9agR2f1Rgg6qgKhIMGzCtYuLsxTeBvYob/Btz13GGFGu9gQSNIVsn4/FXJyksMBc0EcjdjahbZrFB0YEpa9zrlA5CKoZhzjaSRChaKtgNmg090jWRN4MZaeyqxTiTDYMlT6Bgm5zV4h9Zb6i7u4KV0lDbGPQTbhTIRakxS6wsugrZBnNZyrw3n0Xzi9nZbErtJ3MHp3C3jV6dvf1t+vZi/+jF63fns+lbfolVPIExHc3+2peIRNPQU6hSkkZ0wnUnxEpfLqL0ckIAmBGE6TKBstbCpczxcwpu4ptW8NDoOZPZGrFJgkLqBUJ4l2TvygfUd81cOAqNy5fxvyQdQRw06Tw8UaQRhxTyn8si8QFQYJ1/WUIsi5jXCbdXgGeYJ937JYuwdyZwxv+e0Yir8PZy8t3VA9nQOF5hLzwvR4usNeqe2RChojE6pyQT6nA8RI5ocz/sUKFLKs8VPDmFk33rZXzHRrZHdz0KR3e9wmP1+Gobk88fqYLZEl0S+xEWp3sF5IMOJ654AMr3z+k9JPed3sXhhsD3slQOpdcPStQ2c66P7/Vk7wqOHgI4ghNW6WYXawUybPv4G205AVrIQ6r3Elksk30MR35rdHNm7gfx5HDyHGTNM/l0V5t4sYyHsnTpH1rMRNE9JdwGI7ZwoDEAxkWipDqjMQkyWCy+D+Ue3Cw077fgfWAPEaGt/nFE1m3ygFWfTnfdHqbLG/sUaBp03KHXOO0bie9oeO2a+6PZeptflq0fu6xDX35sPIw+HCY/UDJ1mQXHK2ktTf803nbJmM0uDL/BTodBg8q2BudlS8Fh0g+sXa7poHj/w4bNdHd46z9Wpv6HP4UEfxflE4BHNEw+CGLW79Px+IR47WPmzz7o9gh/MYUpz9u+oJj/V7io/92mwYTCSbP5XNFqn8+3E7ijg218OXk6Hl9to//q7XNUzGthLfyMpWhrlxzu6zSM/oGUJbq88qUc9Rst3ZWOyHbpicYDlVgnnDNB2B+PPOPSQwj7zbKUWWef2mO0W5ieOKhu0uhfjOYODA=="
 },
 "probe_client.py": {
  "sha256": "8d7801523cd285f692780bc1422fa611c73ac63773bae741222e4344306207ce",
  "data": "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"
 }
}

root = Path.cwd()
if not (root / "wrangler.jsonc").exists() or not (root / "src" / "app" / "main.py").exists():
    sys.exit("This does not look like the cf-worker folder.\nRun:  cd /workspaces/boltane-workers-poc/files12/cf-worker   and try again.")
target = root.parent / "cf-probe"
if target.exists():
    sys.exit(f"{target} already exists - nothing written. Delete it first if you want a fresh copy.")

failed = False
for name, info in FILES.items():
    raw = zlib.decompress(base64.b64decode(info["data"]))
    if hashlib.sha256(raw).hexdigest() != info["sha256"]:
        print(f"FAIL  {name}: embedded data is corrupted (hash mismatch)")
        failed = True
        continue
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    note = ""
    if path.suffix == ".py":
        try:
            py_compile.compile(str(path), doraise=True, cfile="/tmp/_probe_check.pyc")
            note = "compiles"
        except py_compile.PyCompileError as exc:
            note, failed = f"DOES NOT COMPILE: {exc}", True
    print(f"CREATED   cf-probe/{name}   {note}".rstrip())
print()
if failed:
    sys.exit("FAILED - see above. Delete ../cf-probe before trying again.")
print("Done. Next:  cd ../cf-probe   then   uv run pywrangler deploy")
