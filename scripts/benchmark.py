#!/usr/bin/env python3
"""
SMS Gateway Benchmark — Simulated Load Testing

Simulates concurrent SMS arrivals to measure the gateway's throughput
and latency characteristics under load.

Modes:
  --simulate     Runs against an in-memory queue (no live server needed)
  --live         Runs against a live FastAPI server at --target URL

Metrics:
  • Throughput (messages/second)
  • Latency: P50, P95, P99, Max
  • Error rate
  • Queue depth over time

Usage:
  python benchmark.py --simulate --count 1000 --concurrency 50
  python benchmark.py --live --target http://localhost:8000 --count 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import string
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Add backend to path for simulation mode
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


@dataclass
class BenchmarkResult:
    """Aggregated benchmark results."""
    total_messages: int = 0
    successful: int = 0
    failed: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)

    @property
    def duration_sec(self) -> float:
        return self.end_time - self.start_time

    @property
    def throughput(self) -> float:
        """Messages per second."""
        if self.duration_sec <= 0:
            return 0.0
        return self.successful / self.duration_sec

    @property
    def error_rate(self) -> float:
        if self.total_messages == 0:
            return 0.0
        return self.failed / self.total_messages * 100

    def percentile(self, p: float) -> float:
        """Calculate p-th percentile of latencies."""
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * p / 100)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def max_latency(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def min_latency(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def avg_latency(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return sum(self.latencies_ms) / len(self.latencies_ms)


def generate_sms_payload(index: int) -> dict:
    """Generate a realistic SMS payload for benchmarking."""
    # Simulate Indian phone numbers
    sender = f"+91{''.join(random.choices(string.digits, k=10))}"

    # Simulate OTP messages from common services
    otp_templates = [
        "Your OTP for login is {otp}. Valid for 5 minutes. Do not share. -HDFC Bank",
        "{otp} is your verification code for Amazon. It expires in 10 minutes.",
        "Dear Customer, {otp} is your One Time Password for SBI transaction.",
        "Your Paytm login OTP is {otp}. Do NOT share with anyone.",
        "{otp} - Use this code to verify your WhatsApp phone number.",
        "OTP for PhonePe transaction: {otp}. Valid for 3 min. Don't share.",
        "Your Google verification code is {otp}",
        "{otp} is your Swiggy verification code. Valid for 5 mins.",
    ]

    otp = ''.join(random.choices(string.digits, k=6))
    body = random.choice(otp_templates).format(otp=otp)

    return {
        "sender": sender,
        "body": body,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sms_id": f"bench-{index:06d}",
        "node_id": f"esp32-bench-{random.randint(1, 3):02d}",
        "encrypted": False,
        "priority": random.choice(["high", "normal", "normal", "normal"]),
    }


# ═══════════════════════════════════════════════════════════
#  SIMULATION MODE (No live server)
# ═══════════════════════════════════════════════════════════

async def run_simulation(count: int, concurrency: int) -> BenchmarkResult:
    """
    Run benchmark against an in-memory async queue.
    Tests the pure producer-consumer throughput without network overhead.
    """
    from message_queue import MessageQueue, QueuedMessage

    result = BenchmarkResult(total_messages=count)

    # Mock consumer that simulates processing time
    async def mock_consumer(msg: QueuedMessage) -> bool:
        # Simulate Telegram API latency (5-50ms)
        await asyncio.sleep(random.uniform(0.005, 0.050))
        # 2% simulated failure rate
        if random.random() < 0.02:
            raise Exception("Simulated Telegram 429")
        return True

    queue = MessageQueue(max_size=count + 100, concurrency=concurrency)
    queue.register_consumer(mock_consumer)
    await queue.start()

    # Semaphore to control concurrency
    sem = asyncio.Semaphore(concurrency)

    async def produce_one(index: int):
        async with sem:
            payload = generate_sms_payload(index)
            msg = QueuedMessage(
                sms_id=payload["sms_id"],
                sender=payload["sender"],
                body=payload["body"],
                timestamp=payload["timestamp"],
                node_id=payload["node_id"],
            )
            start = time.perf_counter()
            try:
                success = await queue.enqueue(msg)
                latency_ms = (time.perf_counter() - start) * 1000
                result.latencies_ms.append(latency_ms)
                if success:
                    result.successful += 1
                else:
                    result.failed += 1
            except Exception as e:
                result.failed += 1
                err_name = type(e).__name__
                result.errors[err_name] = result.errors.get(err_name, 0) + 1

    result.start_time = time.time()

    # Fire all producers
    tasks = [produce_one(i) for i in range(count)]

    if RICH_AVAILABLE:
        console = Console()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
        ) as progress:
            task_id = progress.add_task("Sending SMS...", total=count)
            for coro in asyncio.as_completed(tasks):
                await coro
                progress.advance(task_id)
    else:
        batch_size = 100
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i + batch_size]
            await asyncio.gather(*batch)
            print(f"  Progress: {min(i + batch_size, count)}/{count}", end="\r")
        print()

    # Wait for consumers to process
    await asyncio.sleep(2)
    result.end_time = time.time()

    await queue.stop(drain_timeout=10)

    return result


# ═══════════════════════════════════════════════════════════
#  LIVE MODE (Against running server)
# ═══════════════════════════════════════════════════════════

async def run_live(count: int, concurrency: int, target: str) -> BenchmarkResult:
    """Run benchmark against a live FastAPI server."""
    if httpx is None:
        print("ERROR: httpx is required for live mode. Install with: pip install httpx")
        sys.exit(1)

    result = BenchmarkResult(total_messages=count)
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    ) as client:

        async def send_one(index: int):
            async with sem:
                payload = generate_sms_payload(index)
                start = time.perf_counter()
                try:
                    response = await client.post(
                        f"{target}/api/sms/inbound",
                        json=payload,
                    )
                    latency_ms = (time.perf_counter() - start) * 1000
                    result.latencies_ms.append(latency_ms)

                    if response.status_code == 200:
                        result.successful += 1
                    else:
                        result.failed += 1
                        err = f"HTTP_{response.status_code}"
                        result.errors[err] = result.errors.get(err, 0) + 1
                except Exception as e:
                    latency_ms = (time.perf_counter() - start) * 1000
                    result.latencies_ms.append(latency_ms)
                    result.failed += 1
                    err_name = type(e).__name__
                    result.errors[err_name] = result.errors.get(err_name, 0) + 1

        result.start_time = time.time()

        tasks = [send_one(i) for i in range(count)]

        if RICH_AVAILABLE:
            console = Console()
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console,
            ) as progress:
                task_id = progress.add_task(f"Sending SMS to {target}...", total=count)
                for coro in asyncio.as_completed(tasks):
                    await coro
                    progress.advance(task_id)
        else:
            await asyncio.gather(*tasks)

        result.end_time = time.time()

    return result


# ═══════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════

def print_results(result: BenchmarkResult, mode: str, output_file: str = ""):
    """Print formatted benchmark results."""

    if RICH_AVAILABLE:
        console = Console()

        # Header
        console.print()
        console.print(Panel.fit(
            "[bold cyan]📊 SMS Gateway Benchmark Results[/bold cyan]",
            border_style="cyan",
        ))

        # Summary table
        summary = Table(title="Summary", show_header=True, header_style="bold magenta")
        summary.add_column("Metric", style="cyan")
        summary.add_column("Value", justify="right", style="green")

        summary.add_row("Mode", mode.upper())
        summary.add_row("Total Messages", str(result.total_messages))
        summary.add_row("Successful", f"[green]{result.successful}[/green]")
        summary.add_row("Failed", f"[red]{result.failed}[/red]")
        summary.add_row("Error Rate", f"{result.error_rate:.2f}%")
        summary.add_row("Duration", f"{result.duration_sec:.2f}s")
        summary.add_row("Throughput", f"[bold green]{result.throughput:.1f} msg/s[/bold green]")

        console.print(summary)

        # Latency table
        latency = Table(title="Latency Distribution", show_header=True, header_style="bold magenta")
        latency.add_column("Percentile", style="cyan")
        latency.add_column("Latency (ms)", justify="right", style="yellow")

        latency.add_row("Min", f"{result.min_latency:.2f}")
        latency.add_row("P50 (Median)", f"{result.p50:.2f}")
        latency.add_row("P95", f"[yellow]{result.p95:.2f}[/yellow]")
        latency.add_row("P99", f"[red]{result.p99:.2f}[/red]")
        latency.add_row("Max", f"[bold red]{result.max_latency:.2f}[/bold red]")
        latency.add_row("Average", f"{result.avg_latency:.2f}")

        console.print(latency)

        # Errors
        if result.errors:
            errors = Table(title="Error Breakdown", show_header=True, header_style="bold red")
            errors.add_column("Error Type", style="red")
            errors.add_column("Count", justify="right")
            for err, count in sorted(result.errors.items(), key=lambda x: -x[1]):
                errors.add_row(err, str(count))
            console.print(errors)

        console.print()
    else:
        print("\n" + "=" * 60)
        print("  SMS Gateway Benchmark Results")
        print("=" * 60)
        print(f"  Mode:           {mode.upper()}")
        print(f"  Total Messages: {result.total_messages}")
        print(f"  Successful:     {result.successful}")
        print(f"  Failed:         {result.failed}")
        print(f"  Error Rate:     {result.error_rate:.2f}%")
        print(f"  Duration:       {result.duration_sec:.2f}s")
        print(f"  Throughput:     {result.throughput:.1f} msg/s")
        print("-" * 60)
        print("  Latency Distribution:")
        print(f"    Min:    {result.min_latency:.2f}ms")
        print(f"    P50:    {result.p50:.2f}ms")
        print(f"    P95:    {result.p95:.2f}ms")
        print(f"    P99:    {result.p99:.2f}ms")
        print(f"    Max:    {result.max_latency:.2f}ms")
        print(f"    Avg:    {result.avg_latency:.2f}ms")
        if result.errors:
            print("-" * 60)
            print("  Errors:")
            for err, count in sorted(result.errors.items(), key=lambda x: -x[1]):
                print(f"    {err}: {count}")
        print("=" * 60 + "\n")

    # Export to JSON if requested
    if output_file:
        export = {
            "mode": mode,
            "total_messages": result.total_messages,
            "successful": result.successful,
            "failed": result.failed,
            "error_rate_percent": round(result.error_rate, 2),
            "duration_seconds": round(result.duration_sec, 3),
            "throughput_msg_per_sec": round(result.throughput, 1),
            "latency_ms": {
                "min": round(result.min_latency, 2),
                "p50": round(result.p50, 2),
                "p95": round(result.p95, 2),
                "p99": round(result.p99, 2),
                "max": round(result.max_latency, 2),
                "avg": round(result.avg_latency, 2),
            },
            "errors": result.errors,
        }
        Path(output_file).write_text(json.dumps(export, indent=2))
        print(f"  Results exported to: {output_file}")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="SMS Gateway Benchmark — Simulated Load Testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simulate 1,000 concurrent SMS (no server needed)
  python benchmark.py --simulate --count 1000 --concurrency 50

  # Run against a live server
  python benchmark.py --live --target http://localhost:8000 --count 500

  # Export results to JSON
  python benchmark.py --simulate --count 1000 --output results.json
        """,
    )
    parser.add_argument("--simulate", action="store_true", help="Run in simulation mode (no server)")
    parser.add_argument("--live", action="store_true", help="Run against a live server")
    parser.add_argument("--target", default="http://localhost:8000", help="Target server URL (live mode)")
    parser.add_argument("--count", type=int, default=1000, help="Number of SMS messages to simulate")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrent workers")
    parser.add_argument("--output", default="", help="Export results to JSON file")

    args = parser.parse_args()

    if not args.simulate and not args.live:
        args.simulate = True  # Default to simulation

    print(f"\n🚀 Starting benchmark: {args.count} messages, {args.concurrency} workers\n")

    if args.simulate:
        result = asyncio.run(run_simulation(args.count, args.concurrency))
        print_results(result, "simulation", args.output)
    elif args.live:
        result = asyncio.run(run_live(args.count, args.concurrency, args.target))
        print_results(result, "live", args.output)


if __name__ == "__main__":
    main()
