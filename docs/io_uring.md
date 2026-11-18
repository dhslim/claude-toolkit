# io_uring — the Linux async I/O interface, from first principles

A self-contained primer. The throughline is the **"$12 server" benchmark** (Arjay McCandless,
*"I Asked Developers to Max Out a $12 Server"*): the same Twitter-like SQLite API on **1 vCPU / 2 GB**,
every language, held under load for 5 minutes. Every `epoll` entry stalled around **65k–120k** users;
the `io_uring` entries went to **150k, 171k, and a winning 264k**. This doc explains *why* that happened.

> **Platform note:** io_uring is **Linux-only** (kernel 5.1+, May 2019, by Jens Axboe). It does not exist
> on macOS. To actually run anything here, use a Linux box (e.g. your `chart-server` / `gpu-server`).

---

## 1. The problem: syscalls and the readiness model

A normal server does I/O one **syscall** at a time: `read()`, `write()`, `accept()`, `recv()`. Each one
is a user→kernel→user round trip. At 10k+ requests/sec, that overhead (mode switches, cache effects)
becomes the bottleneck — the CPU is busy *asking* to do I/O, not doing work.

`epoll` (the previous best tool, used by most entries in the benchmark) helps but is **readiness-based**:

1. You ask `epoll_wait()` *"which of these sockets are ready?"*
2. It returns a list.
3. You then call `read()`/`write()` on each — **more syscalls**.

So `epoll` tells you *when* you can do I/O without blocking, but **you still do the I/O yourself**, one
syscall per operation. And the old POSIX AIO was effectively broken for buffered file I/O.

## 2. The idea: a completion-based, shared-memory queue

io_uring flips the model. Instead of *"tell me what's ready, then I'll do it,"* you say
**"here are the operations I want done — tell me when each is finished."** That's **completion-based**
(like Windows IOCP), and it works for *both* storage and network I/O.

The mechanism is **two ring buffers in memory shared between your process and the kernel** (set up with
`io_uring_setup()`, then `mmap`'d so both sides see the same memory):

- **Submission Queue (SQ)** — you write requests here.
- **Completion Queue (CQ)** — the kernel writes results here.

```
   YOUR PROCESS                         KERNEL
  ┌───────────────┐                  ┌───────────────┐
  │ fill an SQE   │                  │ pick up SQEs   │
  │ (op, fd, buf) │  ── SQ ring ──▶  │ perform the IO │
  │ advance tail  │   (shared mem)   │ post a CQE     │
  └───────────────┘                  └───────┬───────┘
  ┌───────────────┐                          │
  │ read CQEs,    │  ◀── CQ ring ──          │
  │ match by      │     (shared mem)  ◀──────┘
  │ user_data     │
  └───────────────┘
```

- **SQE** = *Submission Queue Entry*: one operation — an opcode (`IORING_OP_READ`, `_WRITE`, `_ACCEPT`,
  `_RECV`, `_SEND`…), the fd, a buffer pointer, an offset, and a **`user_data`** cookie you choose.
- **CQE** = *Completion Queue Entry*: a result — `res` (bytes transferred, or a negative errno) and the
  same **`user_data`** echoed back so you know *which* request it answers.

Because the rings are shared memory, **submitting a request and reaping a result need no syscall by
themselves** — you just write/read ring slots and advance head/tail pointers. A single
`io_uring_enter()` syscall can submit *many* operations and/or wait for *many* completions. In
**SQPOLL** mode a kernel thread polls the SQ for you, so a busy server can run with **~zero syscalls**
on the hot path.

That is the whole performance thesis: **amortize or eliminate the per-operation syscall.**

## 3. The minimal shape (liburing)

You rarely touch the raw rings; `liburing` is the userspace helper library. A file read looks like:

```c
#include <liburing.h>

struct io_uring ring;
io_uring_queue_init(256, &ring, 0);          // 256-entry rings

struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);   // grab a submission slot
io_uring_prep_read(sqe, fd, buf, len, offset);        // opcode READ, fill fields
io_uring_sqe_set_data(sqe, my_request_ptr);           // user_data cookie

io_uring_submit(&ring);                                // one syscall, submit all pending SQEs

struct io_uring_cqe *cqe;
io_uring_wait_cqe(&ring, &cqe);                        // block until a completion
int nbytes = cqe->res;                                 // result (or -errno)
void *who   = io_uring_cqe_get_data(cqe);              // match it back to your request
io_uring_cqe_seen(&ring, cqe);                         // mark the CQE consumed
```

A **server event loop** is the same idea, scaled: submit an `ACCEPT`; when its CQE arrives, submit a
`RECV` for the new connection; when *that* completes, parse the request, do the DB work, submit a
`SEND`; and so on. One `io_uring_submit_and_wait(&ring, N)` call can push a batch of SQEs and harvest a
batch of CQEs together.

## 4. The features that won the benchmark

Plain rings are already fast. The entries that reached 150k–264k users leaned on four advanced features —
each maps directly to a bottleneck the video calls out.

### a) Registered file tables / direct descriptors — *beating the fd ceiling*
**This is the single biggest lever in the benchmark.** Every process has a file-descriptor limit
(here ~65k). Each open socket costs one fd, so every `epoll` entry hit a wall at **~65k–71k connections**
— the ceiling was never CPU or RAM, it was **fds**.

io_uring can accept sockets as **direct descriptors** that live in **io_uring's own registered file
table**, *not* the process's fd table. They're referenced by an index (`IOSQE_FIXED_FILE`), so they don't
consume ordinary fds. Run several rings, each with its own ~65k-slot table, and you multiply the ceiling:

- Go+io_uring entry: **2 rings → ~131k** slots → 128k users.
- Winning Rust entry: **8 registered tables → 500k+** socket slots → **264k users** (~4× the epoll ceiling).

The process never raised the system-wide limit — it used an I/O mechanism where sockets don't live in the
normal fd table at all. Registering files also skips the kernel's per-op fd lookup/refcount.

### b) Multi-shot operations — *fewer resubmissions*
Normally one SQE = one CQE. **Multishot** accept (`IORING_ACCEPT_MULTISHOT`) and multishot `recv` let a
*single* submitted operation keep producing completions: one `ACCEPT` SQE yields a CQE per incoming
connection; one `RECV` yields a CQE each time data arrives. This "substantially reduces the repeated
bookkeeping of submitting a new operation for every event" (the winner used this).

### c) Completion batching — *amortize overhead across requests*
Instead of handling each completion the instant it lands, wait for a small batch, then do the work once.
The Rust winner targeted **32 completions with a max delay of ~215µs** — spending a tiny bit of latency to
spread per-batch overhead (transaction setup, syscalls) across many requests. Several entries also shared
**one SQLite read transaction per completion batch**.

### d) Fixed/registered buffers — *skip per-op setup*
Pre-register I/O buffers (`io_uring_register_buffers`) so the kernel doesn't map/pin pages on every
operation; used via `READ_FIXED`/`WRITE_FIXED`. Combined with compact per-connection state (the winner's
was a **64-byte record**, with input/output buffers allocated only while actually in use — "even one extra
KB per connection would add ~264 MB at the winning user count").

> Takeaway from the benchmark: past a certain point the language barely mattered — Rust, hand-written
> **assembly**, C, and Go all clustered at the top **because they all used io_uring the same way**. The
> win came from *how I/O and the database were organized*, not raw instruction speed.

## 5. Why it's faster, in one list

- **Fewer syscalls:** batch N operations per `io_uring_enter`; with SQPOLL, ~zero on the hot path.
- **Completion-based:** submit once, get told when done — no "readiness then do-it-yourself" second step.
- **Unified:** same API for disk *and* network (and more: `openat`, `fsync`, `timeout`, `send`/`recv`…).
- **Less kernel bookkeeping:** registered files and buffers remove per-op fd/page work.
- **Chaining:** `IOSQE_IO_LINK` orders dependent ops (e.g. write→fsync) without a user-space round trip.

## 6. Tradeoffs and caveats (don't skip this)

- **Linux-only**, and features are **kernel-version-gated** — multishot accept, direct descriptors, etc.
  arrived across 5.x/6.x. Check `liburing` + the man pages for what your kernel supports.
- **Security history:** io_uring has had a steady stream of serious kernel CVEs (use-after-free,
  priv-esc). Google **disabled it** across Android/ChromeOS/production for a while; kernel 6.6+ added a
  `kernel.io_uring_disabled` sysctl so admins can turn it off. Treat availability as *not guaranteed* in
  hardened environments.
- **Complexity:** it's a lower-level, async, completion-based model. Lifetimes (buffers must outlive the
  operation), ordering, and backpressure are all on you. The benchmark entries were deliberately extreme;
  most production services are fine on a framework + `epoll`.
- **The benchmark's other trick wasn't io_uring at all:** many top entries set SQLite to
  `WAL` + `synchronous=NORMAL`, letting a commit return *before* the log is forced to disk — faster, but a
  power loss can drop a "committed" write. io_uring made the *I/O* cheap; the durability shortcut was
  separate (and the one Quarkus entry was slow precisely because it kept full durability).

## 7. Try it yourself (on a Linux box)

```bash
# on chart-server / gpu-server (Debian/Ubuntu)
sudo apt install liburing-dev
# minimal examples ship with the source:
git clone https://github.com/axboe/liburing && cd liburing && ./configure && make
ls examples/        # io_uring-cp.c, io_uring-test.c, ucontext-cp.c, ...
```

- **liburing** (lib + examples): https://github.com/axboe/liburing
- **"Lord of the io_uring"** tutorial: https://unixism.net/loti/
- **Benchmark code & results:** https://github.com/arjaythedev/twelve-dollar-server-challenge

---

*Mental model to keep:* **`epoll` tells you when you *can* do I/O; io_uring does the I/O and tells you
when it's *done* — in batches, over shared memory, often without a syscall at all.** The $12-server
leaderboard is that one sentence, measured.
