import argparse
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from urllib.parse import unquote, urljoin, urlparse, urlsplit

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


INVALID_NAME_CHARS = r'<>:"/\|?*'
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def app_resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base_path, relative_path)


class DownloadCancelled(Exception):
    pass


@dataclass
class DownloadTask:
    url: str
    file_path: str
    display_path: str


class ProgressReporter:
    def __init__(self, total=0, callback=None, cli_bar=None):
        self.total = total
        self.callback = callback
        self.cli_bar = cli_bar
        self.downloaded = 0
        self.lock = threading.Lock()
        self.started = time.time()
        self.last_emit = 0

    def update(self, amount):
        if amount <= 0:
            return
        with self.lock:
            self.downloaded += amount
            downloaded = self.downloaded
            elapsed = max(time.time() - self.started, 0.001)
            speed = downloaded / elapsed

        if self.cli_bar:
            self.cli_bar.update(amount)

        if self.callback and time.time() - self.last_emit > 0.1:
            self.last_emit = time.time()
            self.callback(downloaded, self.total, speed)

    def reset(self):
        with self.lock:
            self.downloaded = 0
            self.started = time.time()


def safe_name(name):
    name = unquote(name).strip().strip(".")
    name = re.sub(f"[{re.escape(INVALID_NAME_CHARS)}]", "_", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return "download"

    stem = name.split(".")[0].upper()
    if stem in RESERVED_WINDOWS_NAMES:
        name = f"_{name}"

    return name[:180]


def unique_path(path):
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    counter = 2
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def safe_relative_path(path):
    parts = []
    for part in unquote(path).replace("\\", "/").split("/"):
        if not part or part in (".", ".."):
            continue
        parts.append(safe_name(part))
    return os.path.join(*parts) if parts else safe_name(os.path.basename(path))


def same_origin(base_url, candidate_url):
    base = urlparse(base_url)
    candidate = urlparse(candidate_url)
    return (base.scheme, base.netloc) == (candidate.scheme, candidate.netloc)


def canonical_url(url):
    parsed = urlsplit(url)
    return parsed._replace(query="", fragment="").geturl()


def is_under_root(root_url, candidate_url):
    root = urlsplit(canonical_url(root_url))
    candidate = urlsplit(canonical_url(candidate_url))
    root_path = root.path if root.path.endswith("/") else root.path + "/"
    return (
        root.scheme,
        root.netloc,
    ) == (candidate.scheme, candidate.netloc) and candidate.path.startswith(root_path)


def relative_url_path(base_url, candidate_url):
    base_path = urlsplit(canonical_url(base_url)).path
    candidate_path = urlsplit(canonical_url(candidate_url)).path

    if not base_path.endswith("/"):
        base_path = base_path.rsplit("/", 1)[0] + "/"

    if candidate_path.startswith(base_path):
        return candidate_path[len(base_path) :]

    return candidate_path.lstrip("/")


def remote_file_info(url, session):
    try:
        response = session.head(url, allow_redirects=True, timeout=20)
        response.raise_for_status()
        headers = response.headers
    except requests.RequestException:
        response = session.get(url, stream=True, timeout=20)
        response.raise_for_status()
        headers = response.headers
        response.close()

    size = int(headers.get("content-length", 0) or 0)
    accepts_ranges = headers.get("accept-ranges", "").lower() == "bytes"
    return size, accepts_ranges


def stream_download(url, file_path, session, chunk_size, reporter, stop_event):
    part_path = f"{file_path}.part"
    resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    headers = {}
    mode = "wb"

    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        mode = "ab"
        reporter.update(resume_from)

    with session.get(url, headers=headers, stream=True, timeout=30) as response:
        if resume_from and response.status_code != 206:
            mode = "wb"
            reporter.reset()
        response.raise_for_status()

        with open(part_path, mode) as handle:
            for chunk in response.iter_content(chunk_size):
                if stop_event and stop_event.is_set():
                    raise DownloadCancelled()
                if chunk:
                    handle.write(chunk)
                    reporter.update(len(chunk))

    os.replace(part_path, file_path)


def download_segment(url, part_path, start, end, session, chunk_size, reporter, stop_event):
    existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    segment_size = end - start + 1

    if existing >= segment_size:
        reporter.update(segment_size)
        return

    headers = {"Range": f"bytes={start + existing}-{end}"}
    reporter.update(existing)

    with session.get(url, headers=headers, stream=True, timeout=30) as response:
        response.raise_for_status()
        if response.status_code != 206:
            raise RuntimeError("Server stopped honoring range requests")

        with open(part_path, "ab") as handle:
            for chunk in response.iter_content(chunk_size):
                if stop_event and stop_event.is_set():
                    raise DownloadCancelled()
                if chunk:
                    handle.write(chunk)
                    reporter.update(len(chunk))


def multipart_download(
    url,
    file_path,
    session,
    total_size,
    workers,
    chunk_size,
    reporter,
    stop_event,
):
    part_dir = f"{file_path}.parts"
    os.makedirs(part_dir, exist_ok=True)

    segment_size = max(total_size // workers, chunk_size * 32)
    ranges = []
    start = 0
    index = 0
    while start < total_size:
        end = min(start + segment_size - 1, total_size - 1)
        ranges.append((index, start, end))
        start = end + 1
        index += 1

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                download_segment,
                url,
                os.path.join(part_dir, f"{index}.part"),
                start,
                end,
                requests.Session(),
                chunk_size,
                reporter,
                stop_event,
            )
            for index, start, end in ranges
        ]
        for future in as_completed(futures):
            future.result()

    temp_path = f"{file_path}.part"
    with open(temp_path, "wb") as output:
        for index, _, _ in ranges:
            part_path = os.path.join(part_dir, f"{index}.part")
            with open(part_path, "rb") as part:
                while True:
                    chunk = part.read(chunk_size)
                    if not chunk:
                        break
                    output.write(chunk)

    os.replace(temp_path, file_path)
    for index, _, _ in ranges:
        os.remove(os.path.join(part_dir, f"{index}.part"))
    os.rmdir(part_dir)


def download_file(
    url,
    file_path,
    workers=4,
    large_file_mb=50,
    chunk_size=1024 * 256,
    progress_callback=None,
    cli_progress=False,
    retries=3,
    stop_event=None,
):
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    last_error = None

    for attempt in range(1, retries + 1):
        if stop_event and stop_event.is_set():
            raise DownloadCancelled()

        session = requests.Session()
        bar = None
        try:
            total_size, accepts_ranges = remote_file_info(url, session)
            if cli_progress:
                bar = tqdm(
                    total=total_size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=os.path.basename(file_path),
                    ascii=True,
                )

            reporter = ProgressReporter(
                total=total_size, callback=progress_callback, cli_bar=bar
            )

            if accepts_ranges and total_size >= large_file_mb * 1024 * 1024 and workers > 1:
                multipart_download(
                    url,
                    file_path,
                    session,
                    total_size,
                    workers,
                    chunk_size,
                    reporter,
                    stop_event,
                )
                return

            stream_download(url, file_path, session, chunk_size, reporter, stop_event)
            return
        except DownloadCancelled:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** (attempt - 1), 10))
        finally:
            if bar:
                bar.close()

    raise last_error


def discover_files(
    current_url,
    output_dir,
    log=print,
    stop_event=None,
    visited=None,
    root_url=None,
):
    root_url = root_url or current_url
    visited = visited or set()
    normalized_url = canonical_url(current_url).rstrip("/") + "/"
    if normalized_url in visited:
        return []
    visited.add(normalized_url)
    log(f"[*] Scanning: {relative_url_path(root_url, normalized_url) or './'}")

    session = requests.Session()
    try:
        response = session.get(normalized_url, timeout=20)
        response.raise_for_status()
    except Exception as exc:
        log(f"[!] Cannot open {normalized_url}: {exc}")
        return []

    tasks = []
    soup = BeautifulSoup(response.text, "html.parser")

    for anchor in soup.find_all("a", href=True):
        if stop_event and stop_event.is_set():
            raise DownloadCancelled()

        href = anchor["href"].strip()
        if not href or href in ("../", "./") or href.startswith(("?", "#", "mailto:")):
            continue

        full_url = urljoin(normalized_url, href)
        if not same_origin(normalized_url, full_url):
            continue
        if not is_under_root(root_url, full_url):
            continue

        if href.endswith("/"):
            tasks.extend(
                discover_files(
                    full_url,
                    output_dir,
                    log=log,
                    stop_event=stop_event,
                    visited=visited,
                    root_url=root_url,
                )
            )
            continue

        relative_path = safe_relative_path(relative_url_path(root_url, full_url))
        file_path = os.path.join(output_dir, relative_path)
        tasks.append(DownloadTask(full_url, file_path, relative_path))

    return tasks


def download_task(
    task,
    workers,
    large_file_mb,
    retries,
    delay,
    log,
    progress_callback,
    task_callback,
    cli_progress,
    stop_event,
):
    if os.path.exists(task.file_path):
        if task_callback:
            task_callback("skipped", task, "")
        log(f"[=] Already exists: {task.display_path}")
        return "skipped"

    if task_callback:
        task_callback("downloading", task, "")
    log(f"[v] Downloading: {task.display_path}")

    def task_progress(downloaded, total, speed):
        if progress_callback:
            progress_callback(task.display_path, downloaded, total, speed)

    download_file(
        task.url,
        task.file_path,
        workers=workers,
        large_file_mb=large_file_mb,
        progress_callback=task_progress,
        cli_progress=cli_progress,
        retries=retries,
        stop_event=stop_event,
    )
    if delay:
        time.sleep(delay)
    if task_callback:
        task_callback("saved", task, "")
    log(f"[+] Saved: {task.display_path}")
    return "saved"


def crawl_download_queue(
    url,
    local_dir,
    workers=4,
    file_workers=3,
    large_file_mb=50,
    retries=3,
    delay=0,
    log=print,
    progress_callback=None,
    task_callback=None,
    cli_progress=False,
    stop_event=None,
):
    os.makedirs(local_dir, exist_ok=True)
    log("[*] Discovering files...")
    tasks = discover_files(url, local_dir, log=log, stop_event=stop_event)
    log(f"[*] Found {len(tasks)} file(s)")
    if task_callback:
        for task in tasks:
            task_callback("queued", task, "")

    if not tasks:
        return

    completed = 0
    saved = 0
    skipped = 0
    failed = 0
    total = len(tasks)
    progress_lock = threading.Lock()

    def wrapped(task):
        nonlocal completed, saved, skipped, failed
        try:
            result = download_task(
                task,
                workers,
                large_file_mb,
                retries,
                delay,
                log,
                progress_callback,
                task_callback,
                cli_progress and file_workers == 1,
                stop_event,
            )
            with progress_lock:
                completed += 1
                if result == "saved":
                    saved += 1
                else:
                    skipped += 1
                log(f"[*] Progress: {completed}/{total} files")
        except DownloadCancelled:
            raise
        except Exception as exc:
            if task_callback:
                task_callback("failed", task, str(exc))
            with progress_lock:
                completed += 1
                failed += 1
                log(f"[!] Failed: {task.display_path} - {exc}")

    with ThreadPoolExecutor(max_workers=max(1, file_workers)) as executor:
        futures = [executor.submit(wrapped, task) for task in tasks]
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                raise DownloadCancelled()
            future.result()

    log(f"[*] Done. Saved: {saved}, skipped: {skipped}, failed: {failed}")


def crawl(
    url,
    local_dir,
    workers=4,
    file_workers=3,
    large_file_mb=50,
    retries=3,
    delay=0,
    log=print,
    progress_callback=None,
    task_callback=None,
    cli_progress=False,
    stop_event=None,
    visited=None,
):
    crawl_download_queue(
        url,
        local_dir,
        workers=workers,
        file_workers=file_workers,
        large_file_mb=large_file_mb,
        retries=retries,
        delay=delay,
        log=log,
        progress_callback=progress_callback,
        task_callback=task_callback,
        cli_progress=cli_progress,
        stop_event=stop_event,
    )


def human_size(size):
    if not size:
        return "unknown"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class DownloaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Elhacker")
        icon_path = app_resource_path(os.path.join("assets", "elhacker.ico"))
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except tk.TclError:
                pass
        self.geometry("920x620")
        self.minsize(760, 520)

        self.events = queue.Queue()
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.download_items = {}
        self.url_var = tk.StringVar()
        self.output_var = tk.StringVar(value=os.path.abspath("Downloads"))
        self.workers_var = tk.IntVar(value=4)
        self.file_workers_var = tk.IntVar(value=3)
        self.large_file_var = tk.IntVar(value=50)
        self.retries_var = tk.IntVar(value=3)
        self.delay_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="Ready")
        self.counts_var = tk.StringVar(value="Files: 0 found, 0 left")
        self.progress_var = tk.DoubleVar(value=0)
        self.file_counts = {
            "total": 0,
            "queued": 0,
            "downloading": 0,
            "saved": 0,
            "skipped": 0,
            "failed": 0,
        }

        self.build_ui()
        self.after(100, self.drain_events)

    def build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(7, weight=1)

        ttk.Label(root, text="URL").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=self.url_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=4
        )

        ttk.Label(root, text="Output").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", padx=(10, 8), pady=4
        )
        ttk.Button(root, text="Browse", command=self.choose_output).grid(
            row=1, column=2, sticky="ew", pady=4
        )

        settings = ttk.Frame(root)
        settings.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        for column in range(6):
            settings.columnconfigure(column, weight=1)

        ttk.Label(settings, text="Files at once").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            settings, from_=1, to=12, textvariable=self.file_workers_var, width=8
        ).grid(row=0, column=1, sticky="w", padx=(6, 20))
        ttk.Label(settings, text="Speed parts").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(settings, from_=1, to=16, textvariable=self.workers_var, width=8).grid(
            row=0, column=3, sticky="w", padx=(6, 20)
        )

        ttk.Label(settings, text="Boost files over MB").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(
            settings, from_=1, to=10240, textvariable=self.large_file_var, width=8
        ).grid(row=1, column=1, sticky="w", padx=(6, 20), pady=(8, 0))
        ttk.Label(settings, text="Retry attempts").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Spinbox(settings, from_=1, to=10, textvariable=self.retries_var, width=8).grid(
            row=1, column=3, sticky="w", padx=(6, 20), pady=(8, 0)
        )
        ttk.Label(settings, text="Pause seconds").grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Spinbox(settings, from_=0, to=10, increment=0.25, textvariable=self.delay_var, width=8).grid(
            row=1, column=5, sticky="w", padx=(6, 0), pady=(8, 0)
        )

        controls = ttk.Frame(root)
        controls.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.start_button = ttk.Button(controls, text="Start", command=self.start_download)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            controls, text="Stop", command=self.stop_download, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)

        ttk.Label(root, textvariable=self.status_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 4)
        )
        ttk.Label(root, textvariable=self.counts_var).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        ttk.Progressbar(root, variable=self.progress_var, maximum=100).grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10)
        )

        self.content_frame = ttk.Frame(root)
        self.content_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.content_frame.rowconfigure(0, weight=1)
        self.content_frame.columnconfigure(0, weight=1)

        self.intro_frame = ttk.Frame(self.content_frame, padding=(24, 18, 24, 24))
        self.intro_frame.grid(row=0, column=0, sticky="nsew")
        self.intro_frame.columnconfigure(0, weight=1)

        ttk.Label(
            self.intro_frame,
            text="Author: fumioryoto",
            font=("Segoe UI", 9, "bold"),
            anchor="center",
        ).grid(row=0, column=0, sticky="n")
        github_label = ttk.Label(
            self.intro_frame,
            text="GitHub: https://github.com/fumioryoto",
            font=("Segoe UI", 9),
            foreground="#0563c1",
            anchor="center",
            cursor="hand2",
        )
        github_label.grid(row=1, column=0, sticky="n", pady=(2, 0))
        github_label.bind(
            "<Button-1>",
            lambda _event: webbrowser.open_new_tab("https://github.com/fumioryoto"),
        )
        ttk.Label(
            self.intro_frame,
            text=(
                "Paste the directory URL, choose an output folder, adjust the worker "
                "settings if needed, then click Start. Files at once controls how many "
                "different files download together. Speed parts splits large files into "
                "multiple pieces for faster downloading. Boost files over MB decides "
                "which files are large enough to use that speed boost. Retry attempts "
                "tries failed downloads again, and Pause seconds waits between files if "
                "a server needs slower requests. Progress appears in the Downloads and "
                "Activity tabs, and Stop cancels the current run."
            ),
            anchor="center",
            justify="center",
            wraplength=640,
        ).grid(row=2, column=0, sticky="n", pady=(14, 0))

        notebook = ttk.Notebook(root)
        self.notebook = notebook
        notebook.grid(row=0, column=0, sticky="nsew", in_=self.content_frame)
        notebook.grid_remove()

        downloads_tab = ttk.Frame(notebook, padding=(0, 8, 0, 0))
        downloads_tab.rowconfigure(0, weight=1)
        downloads_tab.columnconfigure(0, weight=1)
        notebook.add(downloads_tab, text="Downloads")

        columns = ("status", "file", "progress", "speed")
        self.download_table = ttk.Treeview(
            downloads_tab,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.download_table.heading("status", text="Status")
        self.download_table.heading("file", text="File")
        self.download_table.heading("progress", text="Progress")
        self.download_table.heading("speed", text="Speed")
        self.download_table.column("status", width=110, stretch=False)
        self.download_table.column("file", width=500, stretch=True)
        self.download_table.column("progress", width=130, stretch=False)
        self.download_table.column("speed", width=110, stretch=False)
        self.download_table.grid(row=0, column=0, sticky="nsew")

        table_scroll = ttk.Scrollbar(
            downloads_tab, command=self.download_table.yview
        )
        table_scroll.grid(row=0, column=1, sticky="ns")
        self.download_table.configure(yscrollcommand=table_scroll.set)

        activity_tab = ttk.Frame(notebook, padding=(0, 8, 0, 0))
        activity_tab.rowconfigure(0, weight=1)
        activity_tab.columnconfigure(0, weight=1)
        notebook.add(activity_tab, text="Activity")

        self.log_text = tk.Text(activity_tab, height=8, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(activity_tab, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def choose_output(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get() or os.getcwd())
        if path:
            self.output_var.set(path)

    def log(self, message):
        self.events.put(("log", message))

    def progress(self, *args):
        if len(args) == 4:
            display_path, downloaded, total, speed = args
        else:
            display_path = ""
            downloaded, total, speed = args
        self.events.put(("progress", display_path, downloaded, total, speed))

    def task_event(self, status, task, detail):
        self.events.put(("task", status, task.display_path, detail))

    def start_download(self):
        url = self.url_var.get().strip()
        output = self.output_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Enter a URL to download from.")
            return
        if not output:
            messagebox.showwarning("Missing output", "Choose an output folder.")
            return

        self.stop_event.clear()
        self.progress_var.set(0)
        self.reset_file_counts()
        self.intro_frame.grid_remove()
        self.notebook.grid()
        self.log_text.delete("1.0", "end")
        for item in self.download_table.get_children():
            self.download_table.delete(item)
        self.download_items.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Starting...")

        self.worker_thread = threading.Thread(
            target=self.run_download,
            args=(url, output),
            daemon=True,
        )
        self.worker_thread.start()

    def stop_download(self):
        self.stop_event.set()
        self.status_var.set("Stopping...")

    def run_download(self, url, output):
        try:
            crawl(
                url,
                output,
                workers=max(1, self.workers_var.get()),
                file_workers=max(1, self.file_workers_var.get()),
                large_file_mb=max(1, self.large_file_var.get()),
                retries=max(1, self.retries_var.get()),
                delay=max(0, self.delay_var.get()),
                log=self.log,
                progress_callback=self.progress,
                task_callback=self.task_event,
                stop_event=self.stop_event,
            )
            self.events.put(("done", "Finished"))
        except DownloadCancelled:
            self.events.put(("done", "Stopped"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def reset_file_counts(self):
        for key in self.file_counts:
            self.file_counts[key] = 0
        self.counts_var.set("Files: 0 found, 0 left")

    def refresh_file_counts(self):
        done = (
            self.file_counts["saved"]
            + self.file_counts["skipped"]
            + self.file_counts["failed"]
        )
        left = max(self.file_counts["total"] - done, 0)
        self.counts_var.set(
            "Files: "
            f"{self.file_counts['total']} found, "
            f"{done} done, "
            f"{left} left, "
            f"{self.file_counts['downloading']} active"
        )

    def update_file_counts(self, status):
        if status == "queued":
            self.file_counts["total"] += 1
            self.file_counts["queued"] += 1
        elif status == "downloading":
            if self.file_counts["queued"] > 0:
                self.file_counts["queued"] -= 1
            self.file_counts["downloading"] += 1
        elif status in ("saved", "skipped", "failed"):
            if self.file_counts["downloading"] > 0:
                self.file_counts["downloading"] -= 1
            elif self.file_counts["queued"] > 0:
                self.file_counts["queued"] -= 1
            self.file_counts[status] += 1
        self.refresh_file_counts()

    def update_download_row(self, display_path, status=None, progress=None, speed=None):
        item = self.download_items.get(display_path)
        if not item:
            item = self.download_table.insert(
                "",
                "end",
                values=(status or "Queued", display_path, progress or "", speed or ""),
            )
            self.download_items[display_path] = item
            return

        values = list(self.download_table.item(item, "values"))
        if status is not None:
            values[0] = status
        if progress is not None:
            values[2] = progress
        if speed is not None:
            values[3] = speed
        self.download_table.item(item, values=values)
        if status == "Downloading":
            self.download_table.see(item)

    def drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log_text.insert("end", event[1] + "\n")
                    self.log_text.see("end")
                    if event[1].startswith(("[*] Scanning", "[*] Discovering", "[*] Found")):
                        self.status_var.set(event[1])
                elif kind == "progress":
                    display_path, downloaded, total, speed = event[1:]
                    if total:
                        self.progress_var.set(min(downloaded / total * 100, 100))
                    if display_path:
                        self.update_download_row(
                            display_path,
                            status="Downloading",
                            progress=f"{human_size(downloaded)} / {human_size(total)}",
                            speed=f"{human_size(speed)}/s",
                        )
                    self.status_var.set(
                        f"{human_size(downloaded)} / {human_size(total)} at {human_size(speed)}/s"
                    )
                elif kind == "task":
                    _, status, display_path, detail = event
                    self.update_file_counts(status)
                    labels = {
                        "queued": "Queued",
                        "downloading": "Downloading",
                        "saved": "Saved",
                        "skipped": "Skipped",
                        "failed": "Failed",
                    }
                    label = labels.get(status, status.title())
                    progress = "Done" if status == "saved" else ""
                    if status == "skipped":
                        progress = "Already exists"
                    if status == "failed":
                        progress = detail
                    self.update_download_row(display_path, status=label, progress=progress)
                elif kind == "done":
                    self.status_var.set(event[1])
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.notebook.grid_remove()
                    self.intro_frame.grid()
                elif kind == "error":
                    self.status_var.set("Error")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.notebook.grid_remove()
                    self.intro_frame.grid()
                    messagebox.showerror("Download failed", event[1])
        except queue.Empty:
            pass
        self.after(100, self.drain_events)


def download_with_progress(url, file_path):
    download_file(url, file_path, cli_progress=True)


def main():
    parser = argparse.ArgumentParser(
        description="Elhacker recursive course downloader with CLI and GUI modes"
    )
    parser.add_argument("-u", "--url", help="Base URL to crawl and download from")
    parser.add_argument(
        "-o",
        "--output",
        default="Downloads",
        help="Output directory (will be created if not exists)",
    )
    parser.add_argument(
        "-c",
        "--connections",
        type=int,
        default=4,
        help="Parallel connections per large file",
    )
    parser.add_argument(
        "-j",
        "--file-workers",
        type=int,
        default=3,
        help="Number of files to download at the same time",
    )
    parser.add_argument(
        "--large-file-mb",
        type=int,
        default=50,
        help="Use multi-connection mode for files at least this large",
    )
    parser.add_argument("--retries", type=int, default=3, help="Retries per file")
    parser.add_argument("--delay", type=float, default=0, help="Delay between files")
    parser.add_argument("--gui", action="store_true", help="Open the desktop GUI")

    args = parser.parse_args()

    if args.gui or not args.url:
        DownloaderApp().mainloop()
        return

    print("[+] Starting download")
    print("    App         : Elhacker")
    print("    Author      : fumioryoto")
    print(f"    URL         : {args.url}")
    print(f"    Output      : {args.output}")
    print(f"    File workers: {args.file_workers}")
    print(f"    Connections : {args.connections} per large file\n")

    crawl(
        args.url,
        args.output,
        workers=max(1, args.connections),
        file_workers=max(1, args.file_workers),
        large_file_mb=max(1, args.large_file_mb),
        retries=max(1, args.retries),
        delay=max(0, args.delay),
        cli_progress=True,
    )


if __name__ == "__main__":
    main()
