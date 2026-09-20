FROM python:3.13-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash colony \
    && install -d -m 0755 /run/sshd \
    && install -d -o colony -g colony -m 0700 /home/colony/.ssh

COPY woltspace-*.whl /tmp/
COPY authorized_keys /home/colony/.ssh/authorized_keys

RUN python -m pip install --no-cache-dir /tmp/woltspace-*.whl \
    && rm /tmp/woltspace-*.whl \
    && chown colony:colony /home/colony/.ssh/authorized_keys \
    && chmod 0600 /home/colony/.ssh/authorized_keys \
    && printf '%s\n' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'PermitRootLogin no' \
        'AllowUsers colony' \
        >> /etc/ssh/sshd_config \
    && ssh-keygen -A

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
