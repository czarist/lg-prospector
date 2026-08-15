# Changelog

## 2.0.5

- Cockpit machine panel: CPU model/cores/freq/temps, motherboard + BIOS, GPU clocks/power/fan, disk models.

## 2.0.4

- Mailman never stalls a batch for a missing lane: leftover slots come from the other side (1+3, 0+4, and the reverse).

## 2.0.3

- Mailman batch is 4 emails: 2 niche + 2 generalist, still mixed and domain-diverse.

## 2.0.2

- Mailman forecast: sent/total + ready now + 4-day wait, split by niche and general.
- Niche leads stay eligible for the general template; general-only leads do not get niche mail.
- Cockpit refreshes the queue from MySQL so the numbers move as hunts discover and as the 4-day window opens.

## 2.0.1.1

- Mailman pages past junk niche leads instead of stalling on an empty pair.
- Cockpit shows niche vs generalist queue counts when a pair cannot be formed.
- Host helper script to mask sleep/hibernate and ignore idle power-off.

## 2.0.1

- Split prospecting from sending: hunts only discover, mailman sends mail.
- Mailman sends one niche email and one generalist email per batch.
- Cockpit TUI launches and monitors both hunts plus mailman.
- Host panel: CPU, RAM, GPU usage, principal/SSD/HDD disks, MySQL, Redis, LiteLLM, CRM, and site.
- Log directory is configurable (`LOGS_DIR`); default stays in-repo, HDD path supported.
- Niche email templates rasterize icons and banners to opaque PNGs so Gmail/Outlook render them.

## 0.1.0

- Initial hunt pipeline (discover, enrich, CRM, dispatch).
