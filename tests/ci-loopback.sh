#!/bin/sh
# Run inside the disposable Ubuntu CI container.
set -eu
work=$(mktemp -d /root/omniquad-ci.XXXXXX)
mkdir -p /run/sshd "$work/bin"
ssh-keygen -q -t ed25519 -N '' -f "$work/host"
ssh-keygen -q -t ed25519 -N '' -f "$work/client"
printf '[127.0.0.1]:22222 %s\n' "$(cat "$work/host.pub")" > "$work/known_hosts"
cat > "$work/sshd_config" <<EOF
ListenAddress 127.0.0.1
Port 22222
HostKey $work/host
PidFile $work/server.pid
AuthorizedKeysFile $work/client.pub
AuthenticationMethods publickey
UsePAM yes
AllowUsers root
EOF
export OMNIQUAD_TEST_SSH_CONFIG="$work/client_config"
cat > "$OMNIQUAD_TEST_SSH_CONFIG" <<EOF
Host omni-loopback
    HostName 127.0.0.1
    Port 22222
    IdentityFile $work/client
    IdentitiesOnly yes
    UserKnownHostsFile $work/known_hosts
    StrictHostKeyChecking yes
EOF
cat > "$work/bin/ssh" <<'EOF'
#!/bin/sh
exec /usr/bin/ssh -F "$OMNIQUAD_TEST_SSH_CONFIG" "$@"
EOF
chmod +x "$work/bin/ssh"
/usr/sbin/sshd -f "$work/sshd_config" -E "$work/server.log"
trap 'kill "$(cat "$work/server.pid")"' EXIT
export PATH="$work/bin:$PATH" XDG_STATE_HOME="$work/state"
xvfb-run -a /usr/bin/python3 -B -m unittest discover -s tests -v || {
    cat "$work/server.log"
    exit 1
}
