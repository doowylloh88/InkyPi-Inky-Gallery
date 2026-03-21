"""
nas_scanner.py
──────────────
Network/SMB helpers for the inky_gallery plugin.
Handles subnet detection, host scanning, and SMB authentication.

Requirements:
    pip install smbprotocol netifaces
"""

import os
import socket
import ipaddress
import concurrent.futures
import subprocess

# ── Optional dependencies ────────────────────────────────────

try:
    import smbclient
    SMB_AVAILABLE = True
except ImportError:
    SMB_AVAILABLE = False

# ── Credentials store (in-process only, never written to disk) ──
_working_creds: dict[str, tuple[str, str]] = {}


# ─────────────────────────────────────────────────────────────
# Subnet detection
# ─────────────────────────────────────────────────────────────

def get_local_subnets() -> list[str]:
    """Return CIDR strings for local subnets using the UDP trick."""
    subnets = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        subnets.append(str(net))
        print(f"[NAS] Detected local subnet: {net}")
    except Exception as e:
        print(f"[NAS] Subnet detection failed: {e}")
    return subnets


# ─────────────────────────────────────────────────────────────
# Host scanning
# ─────────────────────────────────────────────────────────────

def _detect_smb(ip: str, timeout: float = 0.4) -> bool:
    """Return True if port 445 or 139 is open."""
    for port in (445, 139):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            pass
    return False


def _resolve_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0].split('.')[0]
    except Exception:
        return ip


def scan_for_nas_hosts(max_workers: int = 64) -> list[dict]:
    """
    Auto-detect local subnets and scan for SMB hosts.
    Returns a list of dicts: {ip, hostname}
    Limits each subnet to the first 512 hosts for speed.
    """
    results = []
    seen = set()

    for cidr in get_local_subnets():
        try:
            network = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            continue

        hosts = list(network.hosts())[:512]

        def probe(ip_obj):
            ip = str(ip_obj)
            if _detect_smb(ip):
                hostname = _resolve_hostname(ip)
                return {"ip": ip, "hostname": hostname}
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for res in ex.map(probe, hosts):
                if res and res["ip"] not in seen:
                    seen.add(res["ip"])
                    results.append(res)

    results.sort(key=lambda h: [int(x) for x in h['ip'].split('.')])
    return results


# ─────────────────────────────────────────────────────────────
# SMB authentication
# ─────────────────────────────────────────────────────────────

def _reset_smb_session(host: str) -> None:
    """Clear any cached session for this host."""
    _working_creds.pop(host, None)
    try:
        smbclient.reset_connection_cache()
    except Exception:
        pass
    try:
        import smbprotocol.connection
        conns = smbprotocol.connection.Connection._connections
        to_del = [k for k in list(conns.keys()) if host in str(k)]
        for k in to_del:
            try:
                conns[k].disconnect()
            except Exception:
                pass
            conns.pop(k, None)
    except Exception:
        pass


def smb_register(host: str, username: str, password: str) -> tuple[bool, str]:
    """
    Try several NTLM auth strategies in order.
    Stores the working credentials for use by smb_list_shares.
    Returns (success, message).
    """
    if not SMB_AVAILABLE:
        return False, "smbprotocol is not installed — run: pip install smbprotocol"

    _reset_smb_session(host)

    candidates = []
    if username:
        candidates.append((username, password))
        if "\\" not in username and "@" not in username:
            candidates.append((f"WORKGROUP\\{username}", password))
            short_host = host.split(".")[0].upper()
            candidates.append((f"{short_host}\\{username}", password))
    candidates.append(("guest", ""))
    candidates.append(("", ""))

    last_err = "Unknown error"
    for user, pwd in candidates:
        try:
            smbclient.register_session(
                host,
                username=user,
                password=pwd,
                auth_protocol="ntlm",
                require_signing=False,
            )
            print(f"[NAS] Connected to {host} as '{user or '(anonymous)'}'")
            _working_creds[host] = (user, pwd)
            return True, "ok"
        except Exception as e:
            last_err = str(e)
            print(f"[NAS] Tried '{user or '(anonymous)'}' — failed: {e}")
            try:
                smbclient.reset_connection_cache()
            except Exception:
                pass

    return False, last_err


def smb_list_shares(host: str) -> list[str]:
    """
    Enumerate shares on a host using stored credentials.
    Falls back to probing the username as a share name (macOS convention).
    Returns a list of share name strings.
    """
    if not SMB_AVAILABLE:
        return []

    user, pwd = _working_creds.get(host, ("", ""))
    bare_user  = user.split("\\")[-1]

    print(f"[NAS] Enumerating shares on {host} as '{user}'")

    # Method 1: smbclient._pool dynamic lookup
    try:
        import smbclient._pool as _pool
        pool_attrs = [x for x in dir(_pool)
                      if 'share' in x.lower() or 'enum' in x.lower()]
        print(f"[NAS] _pool share attrs: {pool_attrs}")

        for fn_name in ("net_share_enum_all", "net_share_enum", "list_shares"):
            fn = getattr(_pool, fn_name, None)
            if fn is None:
                continue
            try:
                entries   = fn(host)
                all_names = [e.name.rstrip("\x00") for e in entries
                             if e.name.rstrip("\x00")]
                visible   = [n for n in all_names if not n.endswith("$")]
                result    = visible if visible else all_names
                print(f"[NAS] Shares via _pool.{fn_name}: {result}")
                return result
            except Exception as e:
                print(f"[NAS] _pool.{fn_name} failed: {e}")
    except Exception as e:
        print(f"[NAS] _pool import failed: {e}")

    # Method 2: impacket
    try:
        from impacket.smbconnection import SMBConnection
        conn = SMBConnection(host, host, sess_port=445, timeout=5)
        conn.login(user, pwd)
        raw       = conn.listShares()
        conn.logoff()
        all_names = [s["shi1_netname"].rstrip("\x00") for s in raw]
        visible   = [n for n in all_names if n and not n.endswith("$")]
        result    = visible if visible else [n for n in all_names if n]
        print(f"[NAS] Shares via impacket: {result}")
        return result
    except ImportError:
        print("[NAS] impacket not installed — skipping")
    except Exception as e:
        print(f"[NAS] impacket failed: {e}")

    # Method 3: macOS home-share convention (username == share name)
    if bare_user:
        print(f"[NAS] Probing macOS home share '{bare_user}'")
        try:
            smbclient.scandir(rf"\\{host}\{bare_user}")
            print(f"[NAS] Home share '{bare_user}' accessible")
            return [bare_user]
        except Exception as e:
            print(f"[NAS] Home share probe failed: {e}")

    print("[NAS] All share enumeration methods failed")
    return []

# ─────────────────────────────────────────────────────────────
# SMB tree browsing
# ─────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff',
                    '.webp', '.heif', '.heic', '.avif'}


def smb_count_images(host: str, share: str, path: str = "") -> int:
    """Count image files directly inside a UNC path (non-recursive, fast)."""
    unc = rf"\\{host}\{share}"
    if path:
        unc = unc + "\\" + path.replace("/", "\\")
    count = 0
    try:
        for entry in smbclient.scandir(unc):
            if entry.name.startswith('.') or entry.is_dir():
                continue
            if os.path.splitext(entry.name)[1].lower() in IMAGE_EXTENSIONS:
                count += 1
    except Exception:
        pass
    return count


def smb_has_children(host: str, share: str, path: str = "") -> bool:
    """Return True if the path contains at least one sub-directory."""
    unc = rf"\\{host}\{share}"
    if path:
        unc = unc + "\\" + path.replace("/", "\\")
    try:
        for entry in smbclient.scandir(unc):
            if not entry.name.startswith('.') and entry.is_dir():
                return True
    except Exception:
        pass
    return False


def smb_tree_children(host: str, share: str, parent_path: str = "") -> list[dict]:
    """
    Return a list of tree-node dicts for direct sub-folders of parent_path.
    Each dict: {name, path, image_count, has_children}
    """
    unc = rf"\\{host}\{share}"
    if parent_path:
        unc = unc + "\\" + parent_path.replace("/", "\\")
    nodes = []
    try:
        for entry in smbclient.scandir(unc):
            if entry.name.startswith('.') or not entry.is_dir():
                continue
            child_path = (parent_path + "/" + entry.name).lstrip("/")
            img_count  = smb_count_images(host, share, child_path)
            has_kids   = smb_has_children(host, share, child_path)
            nodes.append({
                "name":         entry.name,
                "path":         child_path,
                "image_count":  img_count,
                "has_children": has_kids,
            })
    except Exception as e:
        print(f"[NAS] smb_tree_children error at '{parent_path}': {e}")
    nodes.sort(key=lambda n: n["name"].lower())
    return nodes

# ─────────────────────────────────────────────────────────────
# SMB mounting
# ─────────────────────────────────────────────────────────────

def get_mount_point(share: str) -> str:
    """Return the local mount point for a share."""
    return f"/mnt/nas/{share}"


def smb_is_mounted(mount_point: str) -> bool:
    """Return True if the mount point is currently mounted."""
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                if mount_point in line:
                    return True
    except Exception:
        pass
    return False


def smb_mount(host: str, share: str, username: str, password: str) -> tuple[bool, str]:
    """
    Mount an SMB share at /mnt/nas/<share>.
    Creates the mount point directory if needed.
    Returns (success, message).
    """
    mount_point = get_mount_point(share)

    # Already mounted — nothing to do
    if smb_is_mounted(mount_point):
        print(f"[NAS] {mount_point} already mounted")
        return True, mount_point

    # Create mount point if it doesn't exist
    try:
        os.makedirs(mount_point, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create mount point: {e}"

    # Run mount.cifs
    cmd = [
        "sudo", "mount", "-t", "cifs",
        f"//{host}/{share}",
        mount_point,
        "-o", f"username={username},password={password},uid=1000,gid=1000"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            print(f"[NAS] Mounted {host}/{share} at {mount_point}")
            return True, mount_point
        else:
            err = result.stderr.strip() or "Unknown error"
            print(f"[NAS] Mount failed: {err}")
            return False, f"Mount failed: {err}"
    except subprocess.TimeoutExpired:
        return False, "Mount timed out"
    except Exception as e:
        return False, f"Mount error: {e}"
