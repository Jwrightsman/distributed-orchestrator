# Inviting a friend's computer

A checklist for adding **one invited person's machine** as a worker on a Path A
(Tailscale) coordinator. Written for the operator, the person who runs the
coordinator. The friend's own guide is [JOIN.md](JOIN.md). This page is what you
do around it.

Rehearsed on 2026-09-22 against a live Path A coordinator, from a zip of the
commit it ran, up to the installer's consent screen.

Below, `YOUR-HOST.YOUR-TAILNET.ts.net` is the coordinator's tailnet name,
`YOUR-SERVER` is how you reach it over SSH, and `YOUR-SSH-KEY` is your key.

---

## What your friend needs

| | |
| --- | --- |
| **Computer** | Windows, macOS or Linux. **16 GB of memory is comfortable; 8 GB works but is slow** and may need everything else closed. About 4 GB of free disk. |
| **Time** | About 30 minutes, most of it the 3.4 GB model download. |
| **Accounts** | A free Tailscale account (any sign-in: Google, Microsoft, GitHub, email). |
| **Software** | Python 3.12 or newer, Ollama, Tailscale. JOIN.md links all three. |
| **Consent** | They agree to lend their machine, having read what it costs them. The installer asks them directly and will not go ahead without a person at the keyboard. |

## What you send them: three things, two channels

| What | How | Why this way |
| --- | --- | --- |
| **A Tailscale machine share** for the coordinator | A share link from the Tailscale admin page (step 1) | Lets them reach the coordinator and nothing else |
| **The code**, as a zip | Any file transfer (step 2) | The GitHub repository is private, so `git clone` fails for anyone but you |
| **The invitation code** | A private message, ideally one that disappears (step 3) | It is a password. Anyone who can reach the coordinator and has it can enrol a machine |

Plus the address, which is not secret: `https://YOUR-HOST.YOUR-TAILNET.ts.net`

---

## Step 1: share the coordinator machine (not your whole tailnet)

**Use machine sharing, not a tailnet invite.** DEPLOY.md A6 mentions a tailnet
invite, but that puts their device *inside* your tailnet, next to your laptop.
A machine share is narrower in every direction
([Tailscale: sharing](https://tailscale.com/kb/1084/sharing)):

- **They see one machine.** Only the coordinator, not your laptop or anything else.
- **It is one-way.** A shared machine can answer their worker but cannot start a
  connection to their computer. That protects them as well as you.
- **It uses the full name.** Shared machines are reached only as
  `host.tailnet.ts.net`, which is already the address the worker uses and the
  name the certificate is for.
- **It is free on every plan**, and you can revoke it without touching anything else.

In the admin console, [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines):

1. Find the coordinator machine, open its **…** menu, choose **Share…**.
2. Either **Share by email** (their Tailscale sign-in email) or **Copy invite
   link**. Leave "Reusable link" **off**: one link, one person.
3. Send it to them. An unused link expires after 30 days.

They accept it from their own Tailscale account. A brand-new personal account is
the owner of its own tailnet, which is what accepting requires.

## Step 2: send the code as a zip

Until the repository is public, a friend cannot clone it. Make a zip of exactly
the commit the coordinator runs, from your copy of the repository:

```bash
git archive --format=zip --prefix=mycelium/ -o mycelium.zip COMMIT
```

`COMMIT` is what the coordinator is on (`git log --oneline -1` on the VM). The
zip is about 3 MB. Send it any way you like; it contains no secrets (the
coordinator's `data/config.json` is not in the repository).

They unzip it and open a terminal in the `mycelium` folder. JOIN.md, "If you were
sent a zip", covers this step for them.

> **Do not** add them as a GitHub collaborator just to get the code to them. It
> would give them the whole history and every future commit, and cloning a
> private repository needs a GitHub sign-in in their terminal, which is the
> hardest step for someone new to this.

## Step 3: send the invitation code, only when they are ready for it

The invitation code is the coordinator's `node_secret`. Everyone you invite gets
the same code. Each machine swaps it for its own credential when it joins, and
that credential can be revoked on its own.

Send it **when they reach step 6 of the installer** ("Asking for your
invitation code"), not before, and not in the same message as the zip. Put it on your
clipboard without it appearing on screen:

```powershell
(ssh -i YOUR-SSH-KEY root@YOUR-SERVER 'cat /root/distributed-orchestrator/data/config.json' | ConvertFrom-Json).node_secret | Set-Clipboard
```

Paste it into a private message (Signal, iMessage, WhatsApp; turn on
disappearing messages if you can), then clear your clipboard:

```powershell
Set-Clipboard -Value $null
```

They paste it into the installer, where nothing shows as they type. The
installer never takes the code on its command line, so it does not end up in
their shell history.

## Step 4: watch them arrive

When the installer finishes, their machine is enrolled under its own name (their
computer's name plus a short random tail). On the VM:

```bash
cd /root/distributed-orchestrator
docker compose exec orchestrator python /app/scripts/node_enrollment_admin.py --state-dir /data list
```

A new `active` row with their machine's name is the confirmation. When they start
`python node.py --server https://YOUR-HOST.YOUR-TAILNET.ts.net`, the
dashboard's machine count goes up by one and their machine appears on the
**Nodes** page.

## Step 5: the first two-machine run

With both workers online (yours and theirs), pitch one task as **plan & split**.
That is the strategy that breaks a task into pieces and hands them to different
machines at the same time. One shot only ever uses one machine. Watch the **Nodes** page: pieces should land on both machines. That is
the run worth recording for the video ([video-setup.md](video-setup.md)).

## Removing them

Two separate permissions, and each has to be removed on its own. This is the
same rule as DEPLOY.md "Removing somebody".

1. **Mycelium.** If they ran `python worker_installer.py uninstall`, their
   enrollment already revoked itself (`list` shows `revoked`). Otherwise:
   ```bash
   docker compose exec orchestrator python /app/scripts/node_enrollment_admin.py --state-dir /data \
       revoke ENROLLMENT-ID --reason "left the project"
   ```
2. **Tailscale.** In the admin console, open the coordinator's **Share…** and
   revoke their share. They can no longer reach the address at all.

If you ever think the invitation code has leaked, rotate `node_secret`
([SECRET_ROTATION.md](SECRET_ROTATION.md)). Machines that already joined keep
working, because they use their own credentials.

---

## The message to send them

Edit the name, then send it with the share link and the zip. Send the invitation
code separately, later (step 3).

> Hey! I'm building Mycelium, a project where a few computers share the work of
> running a small AI model. Would you lend your computer to it for an evening?
>
> **What it does:** my server sends your computer some text (a prompt). Your
> computer runs an AI model on it and sends the answer back. That's it. Your
> computer doesn't run anything I send, it only reads it as text. I can't see
> your files or anything else on your machine. I can see the answers it
> produces and that it's connected.
>
> **What it costs you:** about 4 GB of disk for the AI model, and your computer
> will be busy (fans on) while it's working on something. 16 GB of memory is
> ideal; 8 GB works if you close other stuff. Setup is about 30 minutes, mostly
> a download. You can stop any time with Ctrl+C, and one command removes it for
> good.
>
> **What you'll need to install:** Python, Ollama (runs the AI model), and
> Tailscale (a private network, so your computer can reach my server without
> anything being open to the internet). The guide links all three.
>
> **What I'm sending:**
> 1. a Tailscale link. It gives you access to my one server and nothing else of
>    mine, and my server can't connect to your computer;
> 2. a zip of the code;
> 3. later, an invitation code. That's a password, so I'll send it separately.
>
> The guide is `docs/JOIN.md` inside the zip. It's written for someone who has
> barely used a terminal. It's honest about the risks too, so read "The honest
> part" at the bottom before you decide. Totally fine to say no.
>
> The address you'll need: `https://YOUR-HOST.YOUR-TAILNET.ts.net`
