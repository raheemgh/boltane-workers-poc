#!/usr/bin/env python3
"""
install_probe2.py - upgrades the throwaway probe Worker in ../cf-probe to version 2 (Stage 6, before touching the Core Engine).

HOW (one step, from the cf-worker folder):
    cd /workspaces/boltane-workers-poc/files12/cf-worker
    python install_probe2.py

All or nothing. It replaces ../cf-probe/src/probe.py (only if it is exactly the version-1 file this chat produced) and adds
../cf-probe/probe_client2.py. probe_client.py and everything in cf-worker are left alone.
Then:
    cd ../cf-probe
    uv run pywrangler deploy                     # same terminal where CLOUDFLARE_API_TOKEN is exported
    python probe_client2.py --url https://boltane-probe.boltane.workers.dev
(about 3-5 minutes; send me the whole output and probe_results2.json)
When the experiments are done, delete the probe:   npx wrangler delete --name boltane-probe
"""
import base64, hashlib, py_compile, sys, zlib
from pathlib import Path

V1_PROBE_SHA256 = "170dc51864c647eac8ae659bffdae025c2689773040abd94167cf5288c91f1c1"

FILES = {'probe_client2.py': {'data': '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',
                      'sha256': '8177f1070e8f44a327d12d04e1bbf0c37a3e6c2c8c2e8d8357e9457fdcbfbb68'},
 'src/probe.py': {'data': 'eNqtV21v4zYS/q5fMdB+iIQ62mTbXgv30r1s6r7g7nYXibtbIAhUWqJtIYqoilSUXJD/fs+Q1IudpMDh6g+2JXKG8/LMM8MwDIPltlGd6MQ91Y1aSfqsmmvZzOhWNrpQFb2h6MKIjaS/xQm9V6Rl1kijZ1QpyoURK6ElP5htUW1o1RoyxQ1kkyD45FUck96qTuZz+lFoc/rxF3onsutNo9oqXwp9rWmrypzMVtLPy+VHaqSuVaUltZUpSvveYBvJKtffUWbukk4U5le7CFvaptK8KRjkhCFVZdJakwn8K7XVwNuw1rQVrVVDXx2RJvx+g9+EPg0Oi0p3eHAHd4r+aKU2WPLiN602tJKBlsaUMqd3ix8/nC/s7jPVSFpUm6KSVGjKtqLawO8gIDpO6PNWYlneicyU97zOIllrDtV6/ZYo+vjhYkmvu/b1m6OZ+/3W/X75NR1+T922yLak1ix2Q+uiKvT2bQzVbxL6WXXsqdXYSGvwAXzrKg4X3ghI2jXVGkkwKy9lQ11htnhBN8UdJ88LejcVEFCKmorKZvQtDrKfwcq/6+/Jqr+WteFtgm5U3paSNqVaiZLGT9SxxtsBD61G3A6pEZnUVKzHk/2h8d5h2XOHZaoy8s7cCoDtzP3/JBocphD9W1G2kmo46XXTulTdvl496s1E0xSwqqh0kUsbrNOLn34hnalaIpa/y+r2d4qAeu+dNsJYqImy9HoXHGbkVWdbyYHIh2SXxbV8gpBOtYB9rqgwcxL6vsoKlchKt41M1y1wLaMO1RjTF7ugjxjLMQrsY9k6EGlxI+n1amPdifaqKwagc/ppAYeLaq1w0piJJLiwbpTFLWei2s0gqlwrsuBxNeGjpPEIP1XJogXqGpkXTBvsvKrgr2lQJPC/28pqtNCLBBZfiF/DW4by9gXB2XOnAZNJEIKjiptaNaYPUf84yX//ioHa/2/bIg+CdaNQK6AdURfkV/biM+tpaUbnDitOqrNMqHspR4yLyjT3tSoqM4M9myIIXoEVHfk1iDPkwChsx2EuazCWrFBfYFcmHXg9MZSis1K1+boUzApNA7njo6M3x/HcxmGMr6US1DBHqxT/Kcr7JEgvlqfLxZzyIjN0Qg+Pwb9Pz/+5OL+Yvjr78Ov75eKcH8IqnNMRXi1/29mx/C39dHo+f6mYTl5YiELbLVLgMpxRLteiLc3Je+QuDgJR1xD0QY3wIshKoTVgf/sZjHNm7ua2XpDaz4wErkPLSQCAslFN1tJwKVUAkcg951FdCiAUdTgfISWMaQrwvEQa674cnjBgwiiyZ8JUSlNwp0nTSMtyPWOFM94Uz3t2IF5IUizAD3zvvh885+iYu6nejTRs0KC6gokTta5Zkd8VDaf4jQiUhbhVxuhDoW/mKBS0Y7RdhQDNmcaEibkfcLSdbsHUMDCILqWsIy/gmMlj4xL6riwaXPeQeSoMcMFoTPgrimcUekm89/8eYRib5BGZFnlkDYBh7nwweFjkIU8BzCEem4Pb7vmSt/DpXJgJf30VxclW3l3Ov716stezgxWYmBdMwjhV6y2sgM4UTkagMbkfOdTsrtm+PC5RG1f0xQkdT7WvwwdW8nj40Efh8KEXOKgOrh5Df2ZP9i+dabE1Q4/VGiSRcgHZXdYWrsShGBa+Wdhm+VKzmFPXoCvzMkPEtULLn8zyG/A42gnX0U7LsAXAxyA4iOhOnAZz4z6ZjG1wzgiwSVweQpYBOJxoCC2MILHBfw8QPE+hggXLbiwzCcKjs6e5H4+wbpz8SSuM7Dm9uUM3f6492jV5l/G4sLA/3PDQ+/BuTvQKYP1DzOndvxZHR8eYRTigtlt7KmYT+5aEaslh9l8aCuDL3NcygjlxkqZMAGn6OKcHvHgML+dfHh1dPQb/72km7Yfi3TLnkv4HeDoBF0WhnQvCeMI+/CLaQ2hhe9nJzhG7Bk7M4L04v1LdE35p65TJxTbgaLKEJDyp/hkds4ibLljKM9lgf600O4DRZ6jTHUdWm2i3GFdD80/tlWBOT8alHa/3CiZcbcK9gtnXmIjc/bOQndEOZp/NqFP6F2XURwQD+vMR6droCSWO7nrTBlILu3b0d0aYFxxkuPHHNsGOLdyUtzv/uwbnycSXfhzG+4ZmLxma/W+GZnuG8mBjjX1q5zjIPDVHv2SO3kOSnzLm/dS4Z6CbHvymxF4jXOiwEMYv+KCnPvRzgp0QdgYuO2g5r8ZpBL7tnHZ5AMGDq3AcwH5wc1q0P8r6EWX01Y5ffobxOidjjBt9plMMTJkYNmxEEi7twpUblej5zyvc1ppCVDwJthQ59IxajLrGJeJkSCnu3RGPa3+iJcNsPaR41DTdgiy6m8oW4KyF2bJDju65wdZ8eeO44wIkbJBLXLr5wjzZwvehioeeFabza3d7wVfjrtl6ONdBYRx/XWEMc6ednxh4IecQo0HU57FtyphkqSX1EmNUpo1zAqZ+FuyH6AiGDkm0w+4YD/Q1XF739PRhBs0g0Db4cfBfJuLeEA==',
                  'sha256': 'ecc056bdcb66eb287de94172776e62863d9bee556f95796ba830e289c5b9c6d2'}}

root = Path.cwd()
if not (root / "wrangler.jsonc").exists() or not (root / "src" / "app" / "main.py").exists():
    sys.exit("This does not look like the cf-worker folder.\nRun:  cd /workspaces/boltane-workers-poc/files12/cf-worker   and try again.")
target = root.parent / "cf-probe"
probe = target / "src" / "probe.py"
if not probe.exists():
    sys.exit(f"{probe} not found - run install_probe.py first (it creates ../cf-probe).")

decoded = {}
for name, info in FILES.items():
    raw = zlib.decompress(base64.b64decode(info["data"]))
    if hashlib.sha256(raw).hexdigest() != info["sha256"]:
        sys.exit(f"FAIL  {name}: embedded data is corrupted (hash mismatch) - nothing written.")
    decoded[name] = raw

current = hashlib.sha256(probe.read_bytes()).hexdigest()
if current == FILES["src/probe.py"]["sha256"]:
    print("src/probe.py is already version 2 - leaving it, refreshing probe_client2.py only.")
    del decoded["src/probe.py"]
elif current != V1_PROBE_SHA256:
    sys.exit("src/probe.py is neither the version-1 file nor version 2 (you changed it?) - nothing written.\n"
             "Send me its content, or delete ../cf-probe and run install_probe.py then this file again.")

failed = False
for name, raw in decoded.items():
    path = target / name
    existed = path.exists()
    path.write_bytes(raw)
    try:
        py_compile.compile(str(path), doraise=True, cfile="/tmp/_probe2_check.pyc")
        note = "compiles"
    except py_compile.PyCompileError as exc:
        note, failed = f"DOES NOT COMPILE: {exc}", True
    print(f"{'REPLACED' if existed else 'CREATED '}   cf-probe/{name}   {note}")
print()
if failed:
    sys.exit("FAILED - see above.")
print("Done. Next:  cd ../cf-probe   then   uv run pywrangler deploy   then   python probe_client2.py --url https://boltane-probe.boltane.workers.dev")
