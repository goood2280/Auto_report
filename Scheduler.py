import os  
import time  
import threading  
import subprocess  
from datetime import datetime, timedelta  
import pandas as pd  
import psutil  
import schedule

# =========================================================  
# CONFIGURATION  
# =========================================================  
PROC_TIMEOUT = 3  
POLL_INTERVAL = 5

PATH_CSV = "data/queue_list.csv"  
PATH_RES = "data/reserved_log.txt"  
PATH_DONE = "data/completed_log.txt"

DATA_GROUPS = {  
    "GRP_1": ["ITM_01", "ITM_02", "ITM_03", "ITM_04", "ITM_05", "ITM_06", "ITM_07", "ITM_08", "ITM_09", "ITM_10"],  
    "GRP_2": ["ITM_11", "ITM_12", "ITM_13"],  
    "GRP_3": ["ITM_14", "ITM_15"],  
}

active_tasks = {}  
shared_lock = threading.Lock()

# =========================================================  
# UTILS  
# =========================================================  
def get_timestamp() -> str:  
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def clean_val(val) -> str:  
    if pd.isna(val):  
        return ""  
    return str(val).strip()

def format_dt(val) -> str:  
    s = clean_val(val)  
    if not s:  
        return ""  
    dt = pd.to_datetime(s, errors="coerce")  
    if pd.isna(dt):  
        return s  
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def generate_key(v1, v2) -> str:  
    return f"{format_dt(v1)} | {clean_val(v2)}"

def init_file(path: str) -> None:  
    dir_p = os.path.dirname(path)  
    if dir_p:  
        os.makedirs(dir_p, exist_ok=True)  
    if not os.path.exists(path):  
        with open(path, "w", encoding="utf-8-sig") as f:  
            pass

def read_keys(path: str) -> set[str]:  
    init_file(path)  
    keys = set()  
    try:  
        with open(path, "r", encoding="utf-8-sig") as f:  
            for line in f:  
                line = line.strip()  
                if not line: continue  
                p = [i.strip() for i in line.split("|")]  
                if len(p) < 4: continue  
                keys.add(generate_key(p[1], p[2]))  
    except Exception:  
        pass  
    return keys

def write_log(path: str, v1, v2, status: str) -> None:  
    init_file(path)  
    line = f"{get_timestamp()} | {format_dt(v1)} | {clean_val(v2)} | {status}\n"  
    with open(path, "a", encoding="utf-8-sig") as f:  
        f.write(line)

# =========================================================  
# EXECUTION  
# =========================================================  
def exec_process(arg: str, v1=None, v2=None, log_path: str | None = None) -> None:  
    start_t = datetime.now()  
    proc = subprocess.Popen(  
        ["python", "Main.py", arg],  
        stdout=subprocess.PIPE,  
        stderr=subprocess.STDOUT,  
        text=True,  
        bufsize=1,  
    )  
    active_tasks[proc.pid] = (proc, start_t, arg)

    try:  
        if proc.stdout:  
            for line in proc.stdout:  
                pass   
        proc.wait()  
    finally:  
        active_tasks.pop(proc.pid, None)

    if proc.returncode == 0:  
        if log_path and v1 is not None and v2 is not None:  
            write_log(log_path, v1, v2, "done")  
    else:  
        pass

def wrap_thread(func, lock: threading.Lock, *args) -> None:  
    if lock.acquire(blocking=False):  
        try:  
            t = threading.Thread(target=func, args=args, daemon=False)  
            t.start()  
            t.join()  
        finally:  
            lock.release()

def monitor_sys() -> None:  
    mem = psutil.virtual_memory()  
    cpu = psutil.cpu_percent(interval=1)

def kill_stale_procs() -> None:  
    now = datetime.now()  
    limit = timedelta(hours=PROC_TIMEOUT)  
    for pid, (proc, start, item) in list(active_tasks.items()):  
        if now - start > limit:  
            try:  
                proc.terminate()  
                try:  
                    proc.wait(5)  
                except subprocess.TimeoutExpired:  
                    proc.kill()  
                active_tasks.pop(pid, None)  
            except Exception:  
                pass

# =========================================================  
# QUEUE MGMT  
# =========================================================  
def get_arg(row: dict) -> str | None:  
    val = clean_val(row.get("id_val", ""))  
    return f"_TRG_{val}" if val else None

def fetch_next_job(csv_p, res_p, done_p) -> dict | None:  
    if not os.path.exists(csv_p):  
        return None  
    try:  
        df = pd.read_csv(csv_p)  
        df["id_val"] = df["id_val"].apply(clean_val)  
        df["status"] = df["status"].astype(str).str.strip().str.lower()  
        df["t_norm"] = df["t_val"].apply(format_dt)  
        df["key"] = df.apply(lambda r: generate_key(r["t_norm"], r["id_val"]), axis=1)

        w_df = df[df["status"] == "wait"].copy()  
        if w_df.empty: return None

        skips = read_keys(res_p) | read_keys(done_p)  
        w_df = w_df[~w_df["key"].isin(skips)].copy()  
        if w_df.empty: return None

        w_df["t_dt"] = pd.to_datetime(w_df["t_norm"], errors="coerce")  
        w_df = w_df.dropna(subset=["t_dt"])  
        if w_df.empty: return None

        w_df = w_df.sort_values(["t_dt", "id_val"], ascending=[True, True])  
        return w_df.iloc[0].to_dict()  
    except Exception:  
        return None

def run_single_job(csv_p, res_p, done_p) -> bool:  
    row = fetch_next_job(csv_p, res_p, done_p)  
    if row is None: return False

    v1, v2 = row.get("t_norm", ""), row.get("id_val", "")  
    key = generate_key(v1, v2)

    if key in read_keys(res_p) or key in read_keys(done_p):  
        return True

    arg = get_arg(row)  
    if not arg: return True

    write_log(res_p, v1, v2, "reserved")  
    try:  
        wrap_thread(exec_process, shared_lock, arg, v1, v2, done_p)  
    except Exception:  
        pass  
    return True

def process_queue(csv_p, res_p, done_p) -> None:  
    while True:  
        time.sleep(POLL_INTERVAL)  
        if not run_single_job(csv_p, res_p, done_p):  
            break

# =========================================================  
# CYCLE MGMT  
# =========================================================  
def run_grp(g_key, c_id, csv_p, res_p, done_p) -> None:  
    items = DATA_GROUPS[g_key]  
    for item in items:  
        wrap_thread(exec_process, shared_lock, item)  
        process_queue(csv_p, res_p, done_p)

def run_cycle(c_no, csv_p, res_p, done_p) -> None:  
    mode = (c_no % 7) + 1  
    targets = ["GRP_1"]  
    if mode >= 4: targets.append("GRP_2")  
    if mode == 7: targets.append("GRP_3")

    process_queue(csv_p, res_p, done_p)  
    for g in targets:  
        run_grp(g, c_no + 1, csv_p, res_p, done_p)

# =========================================================  
# MAIN  
# =========================================================  
schedule.every(10).minutes.do(monitor_sys)  
schedule.every(5).minutes.do(kill_stale_procs)

def main() -> None:  
    init_file(PATH_RES)  
    init_file(PATH_DONE)  
    cycle = 0  
    while True:  
        run_cycle(cycle, PATH_CSV, PATH_RES, PATH_DONE)  
        cycle += 1  
        schedule.run_pending()  
        time.sleep(1)

if __name__ == "__main__":  
    main()  
