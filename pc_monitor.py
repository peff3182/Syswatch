import psutil
import time
import requests
import socket
import threading
import json
from datetime import datetime

# ============================================================
#  CONFIGURATION — Modifie ces valeurs
# ============================================================
NTFY_CHANNEL         = "mon-pc-pascal"        # Canal notifications → ton Android
NTFY_COMMAND_CHANNEL = "mon-pc-pascal-cmd"    # Canal commandes ← ton Android
NTFY_SERVER          = "https://ntfy.sh"

CHECK_INTERVAL       = 10   # Vérification toutes les 10 secondes

# Processus à surveiller (laisser vide [] pour tout surveiller)
# Exemple : WATCH_PROCESSES = ["chrome.exe", "discord.exe", "steam.exe"]
WATCH_PROCESSES = []

# Seuils d'alerte
CPU_ALERT_THRESHOLD  = 90   # % CPU pour déclencher une alerte
RAM_ALERT_THRESHOLD  = 90   # % RAM pour déclencher une alerte
TEMP_ALERT_THRESHOLD = 85   # °C pour déclencher une alerte

# ============================================================

hostname = socket.gethostname()

# ── Helpers ────────────────────────────────────────────────

def send_notification(title, message, priority="high", tags="computer"):
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_CHANNEL}",
            data=message.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     tags
            },
            timeout=10
        )
        print(f"[NOTIF] {title}")
    except Exception as e:
        print(f"[ERREUR] Envoi notification : {e}")


def get_cpu():
    return psutil.cpu_percent(interval=1)


def get_ram():
    m = psutil.virtual_memory()
    return m.percent, m.used / (1024**3), m.total / (1024**3)


def get_disk():
    partitions = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device":   part.device,
                "mountpoint": part.mountpoint,
                "pct":      usage.percent,
                "used_gb":  usage.used  / (1024**3),
                "total_gb": usage.total / (1024**3),
            })
        except Exception:
            pass
    return partitions


def get_temperatures():
    temps = []
    try:
        sensors = psutil.sensors_temperatures()
        if not sensors:
            return []
        for chip, entries in sensors.items():
            for entry in entries:
                if entry.current and entry.current > 0:
                    temps.append({
                        "label": entry.label or chip,
                        "value": entry.current
                    })
    except AttributeError:
        pass
    except Exception as e:
        print(f"[WARN] Températures : {e}")
    return temps


def get_uptime():
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    hours   = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    return f"{hours}h {minutes}min"


def get_process_list():
    """Retourne la liste des processus avec nom + CPU + RAM."""
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            info = p.info
            if info['status'] == 'running' or info['cpu_percent'] > 0:
                procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # Trier par CPU desc
    procs.sort(key=lambda x: x.get('cpu_percent', 0) or 0, reverse=True)
    return procs[:15]  # Top 15


def build_stats_message():
    cpu                  = get_cpu()
    ram_pct, ram_u, ram_t = get_ram()
    temps                = get_temperatures()
    uptime               = get_uptime()

    lines = [
        f"⏱️  Uptime   : {uptime}",
        f"🖥️  CPU      : {cpu:.1f}%",
        f"🧠  RAM      : {ram_pct:.1f}% ({ram_u:.1f} / {ram_t:.1f} Go)",
    ]

    if temps:
        lines.append("🌡️  Températures :")
        for t in temps:
            lines.append(f"     {t['label']} : {t['value']:.1f}°C")

    return "\n".join(lines)


def build_process_message():
    procs = get_process_list()
    lines = ["📋 Top processus actifs :\n"]
    for p in procs[:10]:
        cpu = p.get('cpu_percent') or 0
        mem = p.get('memory_percent') or 0
        lines.append(f"  {p['name'][:25]:<25} CPU:{cpu:5.1f}%  RAM:{mem:4.1f}%")
    return "\n".join(lines)


# ── Surveillance des processus ──────────────────────────────

def monitor_processes(known_procs):
    """Détecte les nouveaux processus et ceux qui se sont arrêtés."""
    current = {p.pid: p.name() for p in psutil.process_iter(['pid', 'name'])
               if True not in [err for err in [False]]}

    new_procs  = {pid: name for pid, name in current.items() if pid not in known_procs}
    dead_procs = {pid: name for pid, name in known_procs.items() if pid not in current}

    # Si WATCH_PROCESSES défini, filtrer
    if WATCH_PROCESSES:
        new_procs  = {pid: name for pid, name in new_procs.items()
                      if any(w.lower() in name.lower() for w in WATCH_PROCESSES)}
        dead_procs = {pid: name for pid, name in dead_procs.items()
                      if any(w.lower() in name.lower() for w in WATCH_PROCESSES)}

    for pid, name in new_procs.items():
        print(f"[PROC] Nouveau : {name} (PID {pid})")
        if WATCH_PROCESSES:  # N'envoyer notif que pour les processus surveillés
            send_notification(
                f"▶️ Processus démarré — {hostname}",
                f"{name} (PID {pid})\n\n{build_stats_message()}",
                priority="default",
                tags="arrow_forward"
            )

    for pid, name in dead_procs.items():
        print(f"[PROC] Arrêté : {name} (PID {pid})")
        if WATCH_PROCESSES:
            send_notification(
                f"⏹️ Processus arrêté — {hostname}",
                f"{name} (PID {pid})",
                priority="low",
                tags="stop_button"
            )

    return current


# ── Surveillance des seuils ─────────────────────────────────

alert_cooldown = {}  # Évite les alertes répétitives

def check_thresholds(cpu, ram_pct, temps):
    now = time.time()

    def can_alert(key, cooldown_sec=300):
        last = alert_cooldown.get(key, 0)
        if now - last > cooldown_sec:
            alert_cooldown[key] = now
            return True
        return False

    if cpu > CPU_ALERT_THRESHOLD and can_alert("cpu"):
        send_notification(
            f"🔥 CPU élevé — {hostname}",
            f"Usage CPU : {cpu:.1f}% (seuil : {CPU_ALERT_THRESHOLD}%)\n\n{build_stats_message()}",
            priority="urgent", tags="warning"
        )

    if ram_pct > RAM_ALERT_THRESHOLD and can_alert("ram"):
        send_notification(
            f"🔥 RAM élevée — {hostname}",
            f"Usage RAM : {ram_pct:.1f}% (seuil : {RAM_ALERT_THRESHOLD}%)\n\n{build_stats_message()}",
            priority="urgent", tags="warning"
        )

    for t in temps:
        if t['value'] > TEMP_ALERT_THRESHOLD and can_alert(f"temp_{t['label']}"):
            send_notification(
                f"🌡️ Température élevée — {hostname}",
                f"{t['label']} : {t['value']:.1f}°C (seuil : {TEMP_ALERT_THRESHOLD}°C)",
                priority="urgent", tags="thermometer"
            )


# ── Écoute des commandes ntfy ───────────────────────────────

def listen_for_commands():
    print(f"[INFO] Écoute commandes sur : {NTFY_COMMAND_CHANNEL}")
    try:
        with requests.get(
            f"{NTFY_SERVER}/{NTFY_COMMAND_CHANNEL}/sse",
            stream=True, timeout=None
        ) as resp:
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8", errors="ignore").lower()

                if "stats"   in decoded:
                    send_notification(f"📊 Stats — {hostname}", build_stats_message(), priority="default")

                elif "cpu"   in decoded:
                    cpu = get_cpu()
                    send_notification(f"🖥️ CPU — {hostname}", f"Usage CPU : {cpu:.1f}%", priority="default")

                elif "ram"   in decoded:
                    pct, u, t = get_ram()
                    send_notification(f"🧠 RAM — {hostname}", f"RAM : {pct:.1f}% ({u:.1f} / {t:.1f} Go)", priority="default")

                elif "temp"  in decoded:
                    temps = get_temperatures()
                    if temps:
                        msg = "\n".join([f"{t['label']} : {t['value']:.1f}°C" for t in temps])
                    else:
                        msg = "Températures non disponibles"
                    send_notification(f"🌡️ Températures — {hostname}", msg, priority="default")

                elif "procs" in decoded or "process" in decoded:
                    send_notification(f"📋 Processus — {hostname}", build_process_message(), priority="default")

                elif "disk"  in decoded or "disque" in decoded:
                    disks = get_disk()
                    msg = "\n".join([f"{d['mountpoint']} : {d['pct']:.1f}% ({d['used_gb']:.1f}/{d['total_gb']:.1f} Go)" for d in disks])
                    send_notification(f"💾 Disques — {hostname}", msg or "Aucun disque trouvé", priority="default")

                elif "uptime" in decoded:
                    send_notification(f"⏱️ Uptime — {hostname}", f"Allumé depuis : {get_uptime()}", priority="default")

                elif "ping"  in decoded:
                    send_notification(f"📡 Pong — {hostname}", f"PC en ligne ✓\n{build_stats_message()}", priority="default")

    except Exception as e:
        print(f"[ERREUR] Commandes : {e}")
        time.sleep(10)
        listen_for_commands()


# ── Main ────────────────────────────────────────────────────

def main():
    print(f"[INFO] PC Monitor démarré sur {hostname}")
    print(f"[INFO] Canal notifications : {NTFY_CHANNEL}")
    print(f"[INFO] Canal commandes     : {NTFY_COMMAND_CHANNEL}")
    if WATCH_PROCESSES:
        print(f"[INFO] Processus surveillés : {', '.join(WATCH_PROCESSES)}")
    else:
        print(f"[INFO] Surveillance : tous les processus (sans notifications individuelles)")

    # Thread commandes
    threading.Thread(target=listen_for_commands, daemon=True).start()

    # Notification de démarrage
    send_notification(
        f"✅ PC démarré — {hostname}",
        f"Le PC vient de démarrer.\n\n{build_stats_message()}",
        priority="default", tags="white_check_mark"
    )

    # Snapshot initial des processus
    known_procs = {p.pid: p.name() for p in psutil.process_iter(['pid', 'name'])}

    while True:
        try:
            cpu                   = get_cpu()
            ram_pct, ram_u, ram_t = get_ram()
            temps                 = get_temperatures()

            # Vérifier les seuils
            check_thresholds(cpu, ram_pct, temps)

            # Surveiller les processus
            known_procs = monitor_processes(known_procs)

        except Exception as e:
            print(f"[ERREUR] Boucle principale : {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
