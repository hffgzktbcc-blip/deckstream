# Glossary

| Term | Meaning here |
|---|---|
| **Deck** | A Steam Deck or other SteamOS handheld. |
| **Daemon** | The user service (`daemon/`) on the Deck that polls the queue, mounts, launches and backs up saves. |
| **Queue** | The list of launch jobs the phone writes and the Deck reads, kept on the site behind your password. |
| **WebDAV** | The file protocol the Deck mounts your game folder over (via rclone). |
| **rclone mount / VFS cache** | The read-only network mount and the local cache that holds the parts of files already read. |
| **Proton / umu-launcher** | The compatibility layer and launcher that run Windows games on SteamOS. |
| **Start-up profile** | The byte ranges a game read on first launch, replayed so later cold launches fetch them first. |
| **Hedge proxy** | Optional: sends each range request twice and uses the first answer. Off by default; unmeasured on a Deck. |
| **Decky** | Decky Loader, which hosts the Gaming Mode quick-access panel (`decky/`). |
| **Doctor** | `doctor.py`, which checks the setup and can write a redacted report bundle. |
| **Measured on a device** | Someone ran it on a Deck and kept the numbers. Not the same as CI passing. |
