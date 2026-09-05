# Joining a Mycelium network

**Written for someone who has barely used a terminal.** If a step assumes
something you do not know, that is a bug in this page — please
[say so](https://github.com/Jwrightsman/distributed-orchestrator/issues/new).

---

## What this actually is

Somebody you know is running a **coordinator**: a program on a server that
takes a plain-English request — "build me a page that charts this data" — and
splits it into pieces.

You are being asked to run a **worker**: a program on your own computer that
takes one of those pieces, runs an AI model on it locally, and sends back the
text the model produced.

That is the whole arrangement. Your computer reads text and writes text.

## What they can and cannot see

**They can see:**

- the text your computer produces in answer to their prompts;
- that your machine is connected, what it has claimed about itself (processor,
  memory, which model it is running), and how much work it has done.

**They cannot see:**

- your files, your other programs, your browsing, your messages;
- anything on your machine outside the worker program itself.

**Your computer does not run anything they send.** It receives their prompt as
text, hands it to the AI model, and returns the answer. It never executes it.
That is not a promise in a document — it is
[a test](../tests/test_contributor_safety.py) that fails the build if it stops
being true.

**Nothing listens for incoming connections.** Your machine dials out to the
coordinator. No port is opened. Your home router does not need changing.

## What it costs you

| | |
| --- | --- |
| **Disk** | About 2.5 GB for the AI model, plus a little for its output |
| **Memory** | 8 GB is the practical minimum for the whole machine |
| **Processor** | Full load, in bursts of a few minutes, while it is building something |
| **Network** | Very little. A real task spends about 2% of its time on the wire |
| **Electricity** | A laptop at full CPU. Roughly what a demanding game costs |

Your machine will be noticeably busy while it is working. That is the deal, and
you can stop at any time.

## What you need before you start

1. **A computer with 8 GB of memory**, running Windows, macOS, or Linux.
2. **Python 3.12 or newer.** Type `python --version` in a terminal to check.
   If it is missing or older, get it from
   [python.org/downloads](https://www.python.org/downloads/). The project is
   tested on 3.14.
3. **Ollama**, the program that runs the AI model on your machine. Get it from
   [ollama.com/download](https://ollama.com/download). The installer here will
   not install it for you — putting software on your computer while you are not
   watching is not something it should do.
4. **Two things from whoever invited you:**
   - the **address** of their coordinator, which must start with `https://`
   - an **invitation code**

> **If the address they gave you starts with `http://`, it will be refused, and
> that is on purpose.** Plaintext means your invitation code and your work
> travel in the clear, readable by anything in between. You cannot turn this
> off, and neither can they — there is no flag for it. Send them
> [docs/DEPLOY.md](DEPLOY.md) and ask for an `https://` address. It costs them
> an afternoon.

## Joining

Open a terminal, put yourself in the project folder, and run:

```bash
python worker_installer.py
```

It walks through nine steps and tells you what each one is doing.

1. **Checks it is not running as an administrator.** It refuses if it is —
   nothing here needs those rights.
2. **Checks your computer and Python version.**
3. **Looks for Ollama.** If it is not there, it tells you where to get it and
   stops.
4. **Asks for the address.** Paste what you were given.
5. **Shows you exactly what is about to happen**, in plain English, including
   every file it will write — and waits. Nothing has been downloaded or
   written yet at this point. Read it. Press Enter to stop.
6. **Asks for the invitation code.** Nothing appears as you type; that is
   normal, and it is so the code does not end up on your screen or in your
   shell history. It is never accepted as part of the command.
7. **Downloads the model** if you do not already have it. This is the slow
   step — 2.5 GB.
8. **Introduces your computer to the coordinator.** It creates one small
   private file that identifies your machine, and registers.
9. **Tells you how to start, how to pause, and how to leave.**

### Starting work

```bash
python node.py --server https://THE-ADDRESS-YOU-WERE-GIVEN
```

It waits for tasks and shows each one as it arrives. **You do not need the
invitation code again** — your machine has its own credential now, and that one
can be revoked without affecting anybody else.

### Stopping

Press **Ctrl+C**, or just close the window. Whatever piece of work you were
holding goes back to the network and is given to someone else. Your machine
disappears from the network within about 90 seconds. There is no penalty, and
credit you have already earned stays earned.

## Leaving for good

```bash
python worker_installer.py uninstall
```

It tells the coordinator you are going, waits for that to be acknowledged, and
deletes your private credential file.

It deliberately does **not** remove:

- **Ollama** — remove it the way you installed it;
- **the model** (about 2.5 GB) — `ollama rm qwen3.5:4b` if you want the space
  back;
- **this folder**, or Python.

Those are yours. It says what it left rather than deciding for you.

Nothing about your firewall, your certificates, or your startup programs was
ever changed, so there is nothing to undo there.

## What gets written to your computer

Exactly two things, and the installer names both before it writes either:

1. **The model**, into Ollama's own storage.
2. **One small file**, in your own configuration directory:

| System | Where |
| --- | --- |
| Windows | `%APPDATA%\Mycelium\nodes\<hash>.json` |
| macOS | `~/Library/Application Support/Mycelium/nodes/<hash>.json` |
| Linux | `~/.config/mycelium/nodes/<hash>.json` |

That file holds a private code identifying your machine to that one
coordinator. It is created readable only by you, and the filename is a hash of
the coordinator's address rather than the address itself.

## When something goes wrong

Every failure prints a sentence and an exit code, not a wall of red text.

| It says | What is happening |
| --- | --- |
| "refusing plaintext http://…" | The address is not `https://`. Ask for an `https://` one. |
| "Ollama is not running" | Install it from [ollama.com](https://ollama.com/download), or start it (`ollama serve` on Linux). |
| "running as root or Administrator" | Close that terminal, open a normal one, run it again. |
| "Your copy of Mycelium is too old" | Yours to fix: `git pull` and run it again. |
| "This coordinator is running older software than your copy" | Not yours to fix. Tell the operator; quote both version numbers. |
| "The coordinator did not accept this computer" | Usually a mistyped invitation code. Nothing was left behind; just run it again. |

## The honest part

This is a small project, not a service. Read
[AGENTS.md](../AGENTS.md) for what it can and cannot do — including that it
produces working output roughly 57% of the time on its own test set, with a
wide error bar it publishes on purpose.

Things you should know before lending a machine:

- **There is no sandbox around the AI model itself.** The worker does not
  execute what the coordinator sends, but the model runs as your user account
  like any other program you install.
- **The operator can see your output.** If you would not want them reading the
  answers your machine produces, do not join their network.
- **You are trusting the person who invited you**, roughly as much as you trust
  anyone whose software you install. The protections here reduce what a
  *stranger on the network* can do to you. They do not make the operator
  harmless.
- **This is an invited alpha.** There are no per-user roles and no hardware
  attestation. See [THREAT_MODEL.md](THREAT_MODEL.md) §"What a contributor is
  and is not exposed to".

If any of that changes your mind, not joining is the right answer and nobody
will mind.
