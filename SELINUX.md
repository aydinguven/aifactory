# SELinux enforcing-mode deployment

AI Factory supports SELinux enforcing mode on Rocky Linux, RHEL, CentOS Stream,
and Fedora. Workspace containers keep SELinux separation enabled; the project
does not disable labels and does not require `setenforce 0` during normal use.

## How it works

- Every workspace bind mount uses Podman's `:Z` option, which assigns a private
  SELinux label usable only by that container.
- The `:U` option maps the host directory ownership to the non-root account in
  the selected workspace image.
- `deploy/configure_selinux.sh` registers `container_file_t` persistently for
  `/var/lib/devcloud/workspaces` and applies the context without overwriting
  private MCS labels belonging to running containers.

Clean installs also place Nginx in front of AI Factory. The installer does not
silently enable a broad httpd_can_network_connect SELinux boolean. If the UI
loads but Nginx returns 502 on an enforcing host, inspect the AVC record and
apply the organization's approved reverse-proxy policy before changing any
global boolean.

## Existing host currently reports Disabled

Do not switch directly from Disabled to Enforcing. Files created while SELinux
was disabled can be unlabeled, so first boot permissive and allow a complete
file-system relabel.

### 1. Update AI Factory and install SELinux tools

```bash
cd /root/devcloud
git pull --ff-only origin main
sudo dnf install -y policycoreutils-python-utils selinux-policy-targeted
sudo DEVCLOUD_SERVICE_USER=root bash deploy/configure_selinux.sh
```

If the installed `devcloud.service` uses a user other than root, replace
`DEVCLOUD_SERVICE_USER=root` with that unit's `User=` value.

### 2. Configure the first enabled boot as permissive

Edit `/etc/selinux/config` and set:

```ini
SELINUX=permissive
SELINUXTYPE=targeted
```

Remove kernel arguments that fully disable SELinux, request a complete relabel,
and reboot:

```bash
sudo grubby --update-kernel ALL --remove-args="selinux=0 enforcing=0"
sudo fixfiles -F onboot
sudo reboot
```

The relabel can make this reboot significantly longer. Do not interrupt it.

### 3. Validate in permissive mode

After the VM returns:

```bash
getenforce
cd /root/devcloud
sudo DEVCLOUD_SERVICE_USER=root bash deploy/configure_selinux.sh
bash deploy/restart.sh
sudo ausearch -m AVC,USER_AVC -ts boot
```

`getenforce` must report `Permissive`. Create a fresh VS Code workspace, open a
terminal, create a file, then repeat with Jupyter and save a notebook.

Containers originally created while SELinux was Disabled may not have private
MCS labels. If an old workspace produces AVC denials, remove only its Podman
container and use the AI Factory **Start** action to recreate it. Its host storage
directory and database record remain intact:

```bash
podman ps -a --format '{{.Names}}'
podman rm -f devcloud-USER-WORKSPACE
```

Replace the example name with one exact `devcloud-...` name from the first
command. Do not delete `/var/lib/devcloud/workspaces`.

### 4. Switch to enforcing

When VS Code, Jupyter, registration, and the Cloudflare hostname work without
unexpected AVC messages:

```bash
sudo setenforce 1
sudo sed -i 's/^SELINUX=.*/SELINUX=enforcing/' /etc/selinux/config
getenforce
sudo ausearch -m AVC,USER_AVC -ts recent
```

`getenforce` must report `Enforcing`. Reboot once more before the demo and repeat
the smoke tests.

## Verification and troubleshooting

```bash
ls -Zd /var/lib/devcloud/workspaces
podman inspect --format '{{range .Mounts}}{{println .Source .Destination .Options}}{{end}}' CONTAINER_NAME
sudo journalctl -u devcloud -n 100 --no-pager
sudo ausearch -m AVC,USER_AVC -ts recent
```

The workspace root should show `container_file_t`, and newly created workspace
mounts should include private relabeling. Diagnose labels and paths before
considering a custom policy. Do not blindly pipe audit messages into
`audit2allow`, and do not disable container separation with
`--security-opt label=disable`.

Official references:

- https://docs.podman.io/en/latest/markdown/podman-run.1.html
- https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_selinux/changing-selinux-states-and-modes
- https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/using_selinux/troubleshooting-problems-related-to-selinux_using-selinux
