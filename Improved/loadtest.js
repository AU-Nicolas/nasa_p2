// Load test for the bully-website service (experiment 1 in ASSIGNMENT.md).
// Sends many HTTP requests to the leader and measures the response time of each one.
//
// Requires Node 18+ (built-in fetch). Run from the repo root:
//   node Improved/loadtest.js
//   node Improved/loadtest.js --url http://localhost/ --requests 2000 --concurrency 1,10,50,100
//
// Where to read the response times:
//   1. The terminal: a summary per concurrency level (min/avg/p50/p95/p99/max, errors, requests/s)
//      and a comparison table at the end.
//   2. Improved/results/loadtest-c<concurrency>-<time>.csv: one row per request with its response
//      time in ms. Open in Excel/Numbers to make charts for the report.

const fs = require("fs");
const path = require("path");

// ---- Settings (override with --name value) ----
const args = parseArgs(process.argv.slice(2));
const URL = args.url ?? "http://localhost/";
const REQUESTS = Number(args.requests ?? 1000); // Requests per concurrency level
const CONCURRENCY = String(args.concurrency ?? "1,10,50").split(",").map(Number); // Requests in flight at once
const TIMEOUT_MS = Number(args.timeout ?? 5000); // A request slower than this counts as failed
const OUT_DIR = path.join(__dirname, "results");

function parseArgs(argv) {
    const out = {};
    for (let i = 0; i < argv.length; i += 2) {
        out[argv[i].replace(/^--/, "")] = argv[i + 1];
    }
    return out;
}

// Sends one request and returns its result; never throws
async function timedRequest(index) {
    const sentAt = new Date().toISOString(); // Wall-clock time, to line up with kubectl logs --timestamps
    const start = performance.now();         // High-resolution timer, used for the response time
    try {
        const res = await fetch(URL, { signal: AbortSignal.timeout(TIMEOUT_MS) });
        await res.text(); // Include the time to download the body
        const ms = performance.now() - start;
        return { index, sentAt, ms, status: res.status, ok: res.ok, error: "" };
    } catch (err) {
        const ms = performance.now() - start;
        return { index, sentAt, ms, status: 0, ok: false, error: err.cause?.code ?? err.name };
    }
}

// Runs REQUESTS requests with `concurrency` workers, each sending its next request as soon as the last one returns
async function runLevel(concurrency) {
    const results = [];
    let next = 0;
    const worker = async () => {
        while (next < REQUESTS) {
            results.push(await timedRequest(next++));
        }
    };

    const start = performance.now();
    await Promise.all(Array.from({ length: concurrency }, worker));
    const totalSeconds = (performance.now() - start) / 1000;

    return { concurrency, results, totalSeconds };
}

// Nearest-rank percentile of a sorted array
function percentile(sorted, p) {
    if (sorted.length === 0) return NaN;
    const rank = Math.ceil((p / 100) * sorted.length);
    return sorted[Math.max(0, rank - 1)];
}

function summarize({ concurrency, results, totalSeconds }) {
    const times = results.filter(r => r.ok).map(r => r.ms).sort((a, b) => a - b);
    const failed = results.length - times.length;
    const avg = times.reduce((sum, t) => sum + t, 0) / times.length;
    return {
        concurrency,
        requests: results.length,
        failed,
        "req/s": results.length / totalSeconds,
        "min ms": times[0],
        "avg ms": avg,
        "p50 ms": percentile(times, 50),
        "p95 ms": percentile(times, 95),
        "p99 ms": percentile(times, 99),
        "max ms": times[times.length - 1],
    };
}

function writeCsv({ concurrency, results }) {
    fs.mkdirSync(OUT_DIR, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const file = path.join(OUT_DIR, `loadtest-c${concurrency}-${stamp}.csv`);
    const rows = results
        .sort((a, b) => a.index - b.index)
        .map(r => [r.index, r.sentAt, r.ms.toFixed(2), r.status, r.ok, r.error].join(","));
    fs.writeFileSync(file, ["index,sent_at,response_ms,status,ok,error", ...rows].join("\n") + "\n");
    return file;
}

// Rounds numbers for printing
function round(row) {
    return Object.fromEntries(
        Object.entries(row).map(([k, v]) => [k, typeof v === "number" && !Number.isInteger(v) ? Number(v.toFixed(1)) : v])
    );
}

async function main() {
    console.log(`Load test: ${URL}, ${REQUESTS} requests per level, concurrency ${CONCURRENCY.join(", ")}\n`);

    // Warm-up so the first measured requests don't include connection setup
    await Promise.all(Array.from({ length: 10 }, (_, i) => timedRequest(i)));

    const summaries = [];
    for (const concurrency of CONCURRENCY) {
        const level = await runLevel(concurrency);
        const summary = round(summarize(level));
        summaries.push(summary);

        console.log(`--- concurrency ${concurrency} ---`);
        console.table(summary);
        console.log(`Per-request times: ${writeCsv(level)}\n`);

        const errors = level.results.filter(r => !r.ok);
        if (errors.length > 0) {
            const counts = {};
            for (const e of errors) counts[e.error || `HTTP ${e.status}`] = (counts[e.error || `HTTP ${e.status}`] ?? 0) + 1;
            console.log("Errors:", counts, "\n");
        }
    }

    console.log("=== Comparison ===");
    console.table(summaries);
}

main();
