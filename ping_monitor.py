#!/usr/bin/env python3
import sys
import os
import csv
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


def _icmp_checksum(data: bytes) -> int:
    """RFC 1071 Internet checksum — the same algorithm the standard `ping`
    command line tool uses to validate ICMP packets."""
    total = 0
    count_to = (len(data) // 2) * 2
    count = 0
    while count < count_to:
        total += data[count + 1] * 256 + data[count]
        total &= 0xffffffff
        count += 2
    if count_to < len(data):
        total += data[-1]
        total &= 0xffffffff
    total = (total >> 16) + (total & 0xffff)
    total += (total >> 16)
    answer = ~total & 0xffff
    answer = (answer >> 8) | ((answer << 8) & 0xff00)
    return answer


def _build_icmp_echo_packet(icmp_id: int, seq: int, payload_size: int) -> bytes:
    """Builds a real ICMP Echo Request packet (type 8) with a checksum and a
    payload of the requested size, using the standard incrementing-byte fill
    pattern (0x08, 0x09, 0x0A, ... wrapping at 0xFF) that `ping` itself uses."""
    header = struct.pack("!BBHHH", 8, 0, 0, icmp_id, seq)
    payload = bytes((0x08 + i) & 0xff for i in range(payload_size))
    chksum = _icmp_checksum(header + payload)
    header = struct.pack("!BBHHH", 8, 0, chksum, icmp_id, seq)
    return header + payload


class PingMonitorApp:
    # Default ICMP echo payload size in bytes — mirrors the -s/-S "packet size"
    # option of the standard `ping` command line tool (e.g. `ping -s 56` on
    # Linux/macOS, `ping -l 56` on Windows). Change this value if you want
    # every probe to send a different amount of payload data.
    PACKET_SIZE_BYTES = 56

    # Default interval between pings, in seconds — mirrors the -i "interval"
    # option of the standard `ping` command line tool (e.g. `ping -i 1`).
    # Change this value to probe more or less frequently.
    PING_INTERVAL_SECONDS = 1.0

    def __init__(self, root):
        self.root = root
        self.root.title("Multi-Host Graphical Ping Monitor")
        self.root.geometry("1150x950")
        self.root.minsize(800, 600)
        self.root.resizable(True, True)

        # 状态追踪
        self.hosts = []
        self.is_monitoring = False
        self.threads = []

        # 数据存储结构: { host: { latencies, sent, received, timeline_stats, tree_id,
        #                          last_status, consecutive_fails, last_down_time } }
        self.host_data = {}

        # Instance copies of the tunables above, in case a future UI control
        # needs to change them per-session without touching the class defaults.
        self.packet_size_bytes = PingMonitorApp.PACKET_SIZE_BYTES
        self.ping_interval_seconds = PingMonitorApp.PING_INTERVAL_SECONDS

        self.setup_ui()

        # 当窗口关闭时安全停止所有线程
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI ---
    def setup_ui(self):
        # --- 顶部控制面板 ---
        control_frame = ttk.LabelFrame(self.root, text=" Network Configuration ", padding=10)
        control_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(control_frame, text="Enter Hosts (comma-separated):").pack(side="left", padx=5)
        self.host_entry = ttk.Entry(control_frame, width=50)
        self.host_entry.pack(side="left", padx=5, fill="x", expand=True)
        self.host_entry.insert(0, "8.8.8.8, 1.1.1.1, google.com")

        self.start_btn = ttk.Button(control_frame, text="Start Monitor", command=self.toggle_monitoring)
        self.start_btn.pack(side="left", padx=5)

        self.export_btn = ttk.Button(control_frame, text="Export Report (.txt)",
                                      command=self.export_report_summary, state="disabled")
        self.export_btn.pack(side="left", padx=5)

        self.export_csv_btn = ttk.Button(control_frame, text="Export Report (.csv)",
                                          command=self.export_report_csv, state="disabled")
        self.export_csv_btn.pack(side="left", padx=5)

        # --- 中部：可拖拽调整大小的三个区域（表格 / 图表 / down hosts）---
        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        # --- 实时数据表格（带滚动条 + 可点击列排序）---
        table_frame = ttk.LabelFrame(paned, text=" Live Connection Dashboard ", padding=10)
        paned.add(table_frame, weight=1)

        table_container = ttk.Frame(table_frame)
        table_container.pack(fill="both", expand=True)

        columns = ("host", "status", "seq", "ttl", "time", "loss")
        self._col_labels = {
            "host": "Host / IP",
            "status": "Last Status",
            "seq": "Sequence",
            "ttl": "TTL / Type",
            "time": "Latency (ms)",
            "loss": "Packet Loss (%)",
        }
        # height=8 -> visible rows before a scrollbar is needed
        self.tree = ttk.Treeview(table_container, columns=columns, show="headings", height=8)

        for col, label in self._col_labels.items():
            self.tree.heading(col, text=label, command=lambda c=col: self._sort_tree_column(c, False))
            self.tree.column(col, anchor="center", width=120)

        tree_scroll = ttk.Scrollbar(table_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # --- 波形图图表（单一组合图，所有 host 一起显示）---
        self.graph_frame = ttk.LabelFrame(paned, text=" Real-Time Latency History ", padding=10)
        paned.add(self.graph_frame, weight=3)

        self.fig, self.ax = plt.subplots(figsize=(10, 4))
        self.ax.set_title("Latency over Time")
        self.ax.set_xlabel("Pings (Index)")
        self.ax.set_ylabel("Latency (ms)")
        self.ax.grid(True, linestyle="--", alpha=0.6)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.graph_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # --- Down Hosts 摘要（带滚动条）---
        down_frame = ttk.LabelFrame(paned, text=" Down Hosts Summary ", padding=10)
        paned.add(down_frame, weight=1)

        self.down_summary_label = ttk.Label(down_frame, text="All hosts up.", foreground="green")
        self.down_summary_label.pack(anchor="w", pady=(0, 5))

        down_table_container = ttk.Frame(down_frame)
        down_table_container.pack(fill="both", expand=True)

        down_columns = ("host", "status", "fails", "loss", "since")
        self.down_tree = ttk.Treeview(down_table_container, columns=down_columns,
                                       show="headings", height=4)
        self.down_tree.heading("host", text="Host / IP")
        self.down_tree.heading("status", text="Status")
        self.down_tree.heading("fails", text="Consecutive Failures")
        self.down_tree.heading("loss", text="Packet Loss (%)")
        self.down_tree.heading("since", text="Last Failed At")
        for col in down_columns:
            self.down_tree.column(col, anchor="center", width=140)

        down_scroll = ttk.Scrollbar(down_table_container, orient="vertical",
                                     command=self.down_tree.yview)
        self.down_tree.configure(yscrollcommand=down_scroll.set)
        self.down_tree.pack(side="left", fill="both", expand=True)
        down_scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------ monitoring ---
    def toggle_monitoring(self):
        if not self.is_monitoring:
            # --- 启动监控流程 ---
            raw_hosts = self.host_entry.get().split(",")
            self.hosts = [h.strip() for h in raw_hosts if h.strip()]

            if not self.hosts:
                messagebox.showerror("Error", "Please input at least one valid host.")
                return

            # Raw ICMP sockets require Administrator (Windows) or root/sudo
            # (macOS/Linux) privileges. Check up front so we fail with a clear
            # message instead of every worker thread silently timing out.
            if not self._has_icmp_permissions():
                messagebox.showerror(
                    "Administrator / Root Required",
                    "Real ICMP ping needs elevated privileges to open a raw socket.\n\n"
                    "Windows: right-click your terminal/IDE and choose 'Run as administrator', "
                    "then run the script again.\n\n"
                    "macOS / Linux: run it with sudo, e.g.:\n"
                    "    sudo python3 ping_monitor.py"
                )
                return

            self.is_monitoring = True
            self.start_btn.config(text="Stop Monitor")
            self.host_entry.config(state="disabled")
            self.export_btn.config(state="disabled")
            self.export_csv_btn.config(state="disabled")

            # 重置表格缓存
            for item in self.tree.get_children():
                self.tree.delete(item)
            for col, label in self._col_labels.items():
                self.tree.heading(col, text=label, command=lambda c=col: self._sort_tree_column(c, False))
            for item in self.down_tree.get_children():
                self.down_tree.delete(item)
            self.down_summary_label.config(text="All hosts up.", foreground="green")
            self.host_data.clear()
            self.ax.clear()

            # 初始化数据池
            for idx, host in enumerate(self.hosts):
                self.host_data[host] = {
                    'latencies': [],
                    'sent': 0,
                    'received': 0,
                    'timeline_stats': [],
                    'log_rows': [],  # structured (seq, timestamp, result, latency_ms) rows for CSV export
                    'tree_id': self.tree.insert("", "end", values=(host, "Starting...", "-", "-", "-", "0%")),
                    'last_status': None,
                    'consecutive_fails': 0,
                    'last_down_time': "-",
                    # Unique 16-bit ICMP identifier per host so replies can be
                    # matched to the host that sent the request even if
                    # several hosts are being pinged at once.
                    'icmp_id': (os.getpid() + idx) & 0xffff,
                }

            # 并发启动多线程
            self.threads = []
            for host in self.hosts:
                t = threading.Thread(target=self.ping_worker, args=(host,), daemon=True)
                t.start()
                self.threads.append(t)

            self.update_plots()
        else:
            # --- 停止监控 ---
            self.is_monitoring = False
            self.start_btn.config(text="Start Monitor")
            self.host_entry.config(state="normal")
            self.threads.clear()

            if self.host_data:
                self.export_btn.config(state="normal")
                self.export_csv_btn.config(state="normal")

            if self.host_data and messagebox.askyesno(
                    "Export Report", "Monitoring stopped. Would you like to export the network summary report?"):
                self.export_report_summary()

    def _has_icmp_permissions(self):
        """Best-effort check that we can actually open a raw ICMP socket."""
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            test_sock.close()
            return True
        except (PermissionError, OSError):
            return False

    @staticmethod
    def _is_down_status(status):
        """A host counts as 'down' for summary/export purposes on any failure
        status, not just a plain timeout."""
        return status in ("Timed Out", "Unreachable", "TTL Expired")

    def ping_worker(self, host):
        local_seq = 0
        icmp_id = self.host_data[host]['icmp_id']

        while self.is_monitoring:
            local_seq += 1
            seq = local_seq & 0xffff
            self.host_data[host]['sent'] += 1
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

            packet = _build_icmp_echo_packet(icmp_id, seq, self.packet_size_bytes)

            status = "Timed Out"
            ttl_val = "-"
            latency = 0

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
                sock.settimeout(1.0)
                # connect() on a raw socket restricts recv() to packets coming
                # from this peer only, so concurrent pings to other hosts
                # don't cross-talk on this socket.
                sock.connect((host, 1))

                start_time = time.time()
                sock.send(packet)

                # Keep reading until we see OUR echo reply (matching id+seq),
                # a real error reply, or the socket times out. This guards
                # against stray/late ICMP packets being mistaken for ours.
                while True:
                    reply = sock.recv(1024)
                    end_time = time.time()

                    if len(reply) < 28:
                        continue  # too short to hold an IP header + ICMP header

                    ttl = reply[8]  # TTL is byte offset 8 in the IPv4 header
                    icmp_type, icmp_code, _chk, r_id, r_seq = struct.unpack("!BBHHH", reply[20:28])

                    if icmp_type == 0 and r_id == icmp_id and r_seq == seq:
                        # Echo Reply matching our request
                        latency = round((end_time - start_time) * 1000, 2)
                        status = "Connected"
                        ttl_val = str(ttl)
                        break

                    if icmp_type in (3, 11) and r_id == icmp_id:
                        # 3 = Destination Unreachable, 11 = Time Exceeded (TTL expired)
                        status = "Unreachable" if icmp_type == 3 else "TTL Expired"
                        break
                    # else: not our reply — keep waiting until timeout

                sock.close()

            except (socket.timeout, PermissionError, OSError):
                status = "Timed Out"
                ttl_val = "-"

            if status == "Connected":
                self.host_data[host]['received'] += 1
                self.host_data[host]['latencies'].append(latency)
                latency_str = f"{latency} ms"

                self.host_data[host]['timeline_stats'].append(
                    f"[{timestamp}] Seq {local_seq}: Success - Latency {latency}ms - TTL {ttl_val}")
                self.host_data[host]['log_rows'].append((local_seq, timestamp, "Success", latency))
                self.host_data[host]['consecutive_fails'] = 0
            else:
                latency_str = "Timeout" if status == "Timed Out" else status
                self.host_data[host]['latencies'].append(0)

                self.host_data[host]['timeline_stats'].append(
                    f"[{timestamp}] Seq {local_seq}: FAILED - {status}")
                self.host_data[host]['log_rows'].append((local_seq, timestamp, status, ""))
                self.host_data[host]['consecutive_fails'] += 1
                self.host_data[host]['last_down_time'] = timestamp

            self.host_data[host]['last_status'] = status

            sent = self.host_data[host]['sent']
            received = self.host_data[host]['received']
            loss_pct = f"{round(((sent - received) / sent) * 100, 1)}%"

            tree_id = self.host_data[host]['tree_id']
            row_values = (host, status, str(local_seq), ttl_val, latency_str, loss_pct)
            self.root.after(0, self._update_row, tree_id, row_values)

            time.sleep(self.ping_interval_seconds)

    def _update_row(self, tree_id, values):
        """Runs on the main Tk thread (scheduled via root.after) to safely update a table row."""
        if self.tree.exists(tree_id):
            self.tree.item(tree_id, values=values)

    def _sort_tree_column(self, col, reverse):
        """Click-to-sort handler shared by every column heading in the dashboard."""
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]

        def sort_key(pair):
            raw = pair[0].strip()
            # Strip common suffixes ("%", "ms") so numeric columns sort numerically,
            # not alphabetically (e.g. "9 ms" before "10 ms").
            cleaned = raw.replace("%", "").replace("ms", "").strip()
            try:
                return (0, float(cleaned))
            except ValueError:
                return (1, raw.lower())

        items.sort(key=sort_key, reverse=reverse)

        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)

        # Clicking the same column again flips the direction; update arrow indicators.
        for c, label in self._col_labels.items():
            arrow = ""
            if c == col:
                arrow = " \u25bc" if reverse else " \u25b2"
            self.tree.heading(c, text=label + arrow)
        self.tree.heading(col, command=lambda: self._sort_tree_column(col, not reverse))

    # ------------------------------------------------------------- summaries ---
    def update_down_summary(self):
        for item in self.down_tree.get_children():
            self.down_tree.delete(item)

        down_hosts = [(h, d) for h, d in self.host_data.items() if self._is_down_status(d.get('last_status'))]

        if not down_hosts:
            self.down_summary_label.config(text="All hosts up.", foreground="green")
            return

        names = ", ".join(h for h, _ in down_hosts)
        self.down_summary_label.config(
            text=f"{len(down_hosts)} host(s) currently down: {names}", foreground="red")

        for host, data in down_hosts:
            sent = data['sent']
            received = data['received']
            loss_pct = f"{round(((sent - received) / sent) * 100, 1)}%" if sent else "0%"
            self.down_tree.insert("", "end", values=(
                host, "Down", data.get('consecutive_fails', 0), loss_pct, data.get('last_down_time', '-')))

    def export_report_summary(self):
        """生成结构化的网络丢包与延迟计算分析总结文件"""
        if not self.host_data:
            messagebox.showwarning("No Data", "There is no monitoring data to export yet.")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("Log Files", "*.log"), ("All Files", "*.*")],
            title="Save Packet Summary Report"
        )

        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("=" * 65 + "\n")
                f.write("           NETWORK PERFORMANCE SUMMARY REPORT\n")
                f.write(f"           Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 65 + "\n\n")

                f.write("[1. HOST AGGREGATED METRICS]\n")
                f.write("-" * 80 + "\n")
                f.write(f"{'Target Host':<20} | {'Sent':<6} | {'Recv':<6} | {'Loss %':<8} | "
                        f"{'Min (ms)':<9} | {'Max (ms)':<9} | {'Avg (ms)':<9}\n")
                f.write("-" * 80 + "\n")

                for host, data in self.host_data.items():
                    sent = data['sent']
                    received = data['received']
                    loss_pct = round(((sent - received) / sent) * 100, 1) if sent > 0 else 0.0

                    valid_latencies = [l for l in data['latencies'] if l > 0]
                    if valid_latencies:
                        min_lat = min(valid_latencies)
                        max_lat = max(valid_latencies)
                        avg_lat = round(sum(valid_latencies) / len(valid_latencies), 2)
                    else:
                        min_lat, max_lat, avg_lat = "-", "-", "-"

                    f.write(f"{host:<20} | {sent:<6} | {received:<6} | {str(loss_pct) + '%':<8} | "
                            f"{min_lat:<9} | {max_lat:<9} | {avg_lat:<9}\n")
                f.write("-" * 80 + "\n\n")

                f.write("[2. HOSTS DOWN AT TIME OF EXPORT]\n")
                f.write("-" * 45 + "\n")
                down_now = [h for h, d in self.host_data.items() if self._is_down_status(d.get('last_status'))]
                if down_now:
                    for h in down_now:
                        f.write(f"- {h} (last failed at {self.host_data[h].get('last_down_time', '-')})\n")
                else:
                    f.write("All hosts were up.\n")
                f.write("\n")

                f.write("[3. DETAILED SEQUENCE TIMELINE LOGS]\n")
                for host, data in self.host_data.items():
                    f.write(f"\n# Target Stream: {host}\n")
                    f.write("-" * 45 + "\n")
                    for log_entry in data['timeline_stats']:
                        f.write(log_entry + "\n")

            messagebox.showinfo("Success", f"Report successfully exported to:\n{file_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to write file summary:\n{str(e)}")

    def export_report_csv(self):
        """Export a properly formatted CSV report that opens cleanly in Excel."""
        if not self.host_data:
            messagebox.showwarning("No Data", "There is no monitoring data to export yet.")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
            title="Save Packet Summary Report (CSV)"
        )

        if not file_path:
            return

        try:
            # utf-8-sig adds a BOM so Windows Excel auto-detects UTF-8 correctly
            # instead of mangling any special characters.
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)

                writer.writerow(["Network Performance Summary Report"])
                writer.writerow(["Generated on", time.strftime("%Y-%m-%d %H:%M:%S")])
                writer.writerow([])

                # --- Section 1: one row per host, all key metrics as their own columns ---
                writer.writerow(["Host Aggregated Metrics"])
                writer.writerow(["Host", "Sent", "Received", "Loss %", "Min (ms)", "Max (ms)",
                                  "Avg (ms)", "Last Status", "Consecutive Fails", "Last Down At"])
                for host, data in self.host_data.items():
                    sent = data['sent']
                    received = data['received']
                    loss_pct = round(((sent - received) / sent) * 100, 1) if sent > 0 else 0.0

                    valid_latencies = [l for l in data['latencies'] if l > 0]
                    if valid_latencies:
                        min_lat = min(valid_latencies)
                        max_lat = max(valid_latencies)
                        avg_lat = round(sum(valid_latencies) / len(valid_latencies), 2)
                    else:
                        min_lat, max_lat, avg_lat = "", "", ""

                    writer.writerow([
                        host, sent, received, loss_pct, min_lat, max_lat, avg_lat,
                        data.get('last_status') or "-", data.get('consecutive_fails', 0),
                        data.get('last_down_time', '-')
                    ])
                writer.writerow([])

                # --- Section 2: hosts currently down, isolated for a quick filtered view ---
                writer.writerow(["Hosts Currently Down"])
                down_now = [(h, d) for h, d in self.host_data.items() if self._is_down_status(d.get('last_status'))]
                if down_now:
                    writer.writerow(["Host", "Consecutive Fails", "Loss %", "Last Down At"])
                    for host, data in down_now:
                        sent = data['sent']
                        received = data['received']
                        loss_pct = round(((sent - received) / sent) * 100, 1) if sent else 0.0
                        writer.writerow([host, data.get('consecutive_fails', 0), loss_pct,
                                          data.get('last_down_time', '-')])
                else:
                    writer.writerow(["All hosts were up at time of export."])
                writer.writerow([])

                # --- Section 3: every individual ping, one row each (sortable/filterable in Excel) ---
                writer.writerow(["Detailed Sequence Timeline Log"])
                writer.writerow(["Host", "Sequence", "Timestamp", "Result", "Latency (ms)"])
                for host, data in self.host_data.items():
                    for seq, timestamp, result, latency in data['log_rows']:
                        writer.writerow([host, seq, timestamp, result, latency])

            messagebox.showinfo("Success", f"CSV report successfully exported to:\n{file_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to write CSV report:\n{str(e)}")

    def update_plots(self):
        if not self.is_monitoring:
            return

        self.ax.clear()
        self.ax.set_title("Live Latency Trends (ms)")
        self.ax.set_xlabel("Pings (Last 30 packets)")
        self.ax.set_ylabel("Latency (ms)")
        self.ax.grid(True, linestyle="--", alpha=0.5)

        has_data = False
        for host, data in self.host_data.items():
            y_data = data['latencies'][-30:]
            x_data = list(range(len(y_data)))
            if y_data:
                has_data = True
                self.ax.plot(x_data, y_data, marker="o", linestyle="-", label=host)

        if has_data:
            self.ax.legend(loc="upper left", fontsize=8)

        self.canvas.draw()

        self.update_down_summary()

        self.root.after(1000, self.update_plots)

    def on_close(self):
        self.is_monitoring = False
        self.root.destroy()


def main():
    root = tk.Tk()
    app = PingMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()