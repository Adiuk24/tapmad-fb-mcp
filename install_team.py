#!/usr/bin/env python3
"""Install the Tapmad social MCP into a teammate's Claude. One command, and the
teammate never sees or types a token.

    python3 install_team.py

Credentials come from `tapmad-team.env` sitting next to this script. That file
is gitignored and ships only in the private bundle handed to the BD team — this
repo is public, so a token committed here would be scraped within minutes and
auto-revoked by Meta (GitHub's secret scanning is a Meta partner). Splitting it
this way is what keeps ONE shared token alive for the whole team.

Re-running is safe: it rewrites its own entry, backs up each config first, and
leaves every other MCP server alone.
"""
import json, os, platform, shutil, subprocess, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = "git+https://github.com/Adiuk24/tapmad-fb-mcp"
ENTRY = "tapmad-fb"                       # key under "mcpServers"
VENV = Path.home() / ".tapmad-fb-mcp"     # own venv: no PATH guessing, no clashes
HERE = Path(__file__).resolve().parent
WIN = platform.system() == "Windows"


def load_env(path):
    """KEY=VALUE lines -> dict. Blanks and #comments skipped. Split on the FIRST
    '=' only: Meta tokens and Google sheet ids both contain '='."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def verify(token):
    """Check the token BEFORE touching any config. The whole team shares one
    token, so 'Claude shows no tools' must never be ambiguous between a broken
    install and a revoked token — this turns the second case into a sentence."""
    url = "https://graph.facebook.com/v22.0/debug_token?" + urllib.parse.urlencode(
        {"input_token": token, "access_token": token})
    try:
        d = json.load(urllib.request.urlopen(url, timeout=30)).get("data", {})
    except urllib.error.HTTPError:
        # A dead token is a 400 from Graph, not a network fault. Reporting it as
        # one would send the teammate hunting their wifi while the real answer is
        # "the token was rotated" — the one thing this check exists to say.
        d = {}
    except Exception as e:
        sys.exit(f"✗ cannot reach the Facebook Graph API to check the token: {e}")
    if not d.get("is_valid"):
        sys.exit("✗ the shared Tapmad token is no longer valid (revoked or rotated).\n"
                 "  Ask for a fresh tapmad-team.env — do not hand-edit this one.")
    exp = d.get("expires_at")
    print(f"✓ token valid — {d.get('type')}, "
          f"{'never expires' if not exp else 'expires ' + str(exp)}, "
          f"{len(d.get('scopes') or [])} scopes")


def install():
    """Install the server into its own venv and return the executable Claude
    spawns. A dedicated venv beats `pip install --user` here: the path is known
    up front, so the config entry never depends on the teammate's PATH."""
    py = VENV / ("Scripts/python.exe" if WIN else "bin/python")
    if not py.exists():
        print(f"· creating {VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    print("· installing the MCP server (needs git + network)")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", REPO], check=True)
    exe = VENV / ("Scripts/tapmad-fb-mcp.exe" if WIN else "bin/tapmad-fb-mcp")
    if not exe.exists():
        sys.exit(f"✗ install finished but {exe} is missing — report this, do not retry blindly")
    return exe


def config_paths():
    """Every Claude config on this machine. Claude Code and Claude Desktop keep
    separate files and a teammate may use either, so write to whichever exist."""
    home = Path.home()
    out = [home / ".claude.json"]                                    # Claude Code
    if WIN:
        out.append(Path(os.environ.get("APPDATA", home)) / "Claude/claude_desktop_config.json")
    elif platform.system() == "Darwin":
        out.append(home / "Library/Application Support/Claude/claude_desktop_config.json")
    else:
        out.append(home / ".config/Claude/claude_desktop_config.json")
    return out


def register(path, exe, env):
    """Merge our entry into one config. Never clobbers the file: unrelated MCP
    servers, and every other setting, are read back and rewritten untouched."""
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print(f"  ! {path} is not valid JSON — skipped, fix it by hand")
            return False
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    elif path.name == "claude_desktop_config.json":
        return False                       # Claude Desktop not installed here
    else:
        cfg = {}
    cfg.setdefault("mcpServers", {})[ENTRY] = {
        "type": "stdio", "command": str(exe), "args": [], "env": env}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"  ✓ registered in {path}")
    return True


def main():
    envfile = Path(sys.argv[sys.argv.index("--env") + 1]) if "--env" in sys.argv \
        else HERE / "tapmad-team.env"
    if not envfile.exists():
        sys.exit(f"✗ {envfile.name} not found next to this script.\n"
                 "  This public repo ships the installer only. Get the private\n"
                 "  bundle (installer + tapmad-team.env) from the Tapmad BD lead.")
    env = load_env(envfile)
    if not env.get("FB_ACCESS_TOKEN"):
        sys.exit(f"✗ {envfile.name} has no FB_ACCESS_TOKEN line")

    verify(env["FB_ACCESS_TOKEN"])
    exe = install()
    if not sum(register(p, exe, env) for p in config_paths()):
        sys.exit("✗ found no Claude config to write to — is Claude installed?")
    print("\nDone. Quit Claude completely and reopen it; ask it to 'list the "
          "Tapmad Facebook pages' to confirm the tools are live.")


if __name__ == "__main__":
    main()
