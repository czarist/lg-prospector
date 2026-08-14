# Changelog

## 2.0.1

- Split prospecting from sending: hunts only discover, mailman sends mail.
- Mailman sends one niche email and one generalist email per batch.
- Cockpit TUI launches and monitors both hunts plus mailman.
- Host panel: CPU, RAM, GPU usage, principal/SSD/HDD disks, MySQL, Redis, LiteLLM, CRM, and site.
- Log directory is configurable (`LOGS_DIR`); default stays in-repo, HDD path supported.
- Niche email templates rasterize icons and banners to opaque PNGs so Gmail/Outlook render them.

## 0.1.0

- Initial hunt pipeline (discover, enrich, CRM, dispatch).
