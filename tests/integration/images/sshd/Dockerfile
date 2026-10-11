# An ssh target for the CI-cancel test: deploy-action runs
# `ssh host "cd dir && flow-deploy deploy --tag X"` with no pty.
FROM alpine:3.20
RUN apk add --no-cache openssh-server python3 \
    && ssh-keygen -A \
    && adduser -D -s /bin/sh deploy \
    && echo 'deploy:*' | chpasswd -e \
    && mkdir -p /home/deploy/.ssh \
    && chmod 700 /home/deploy/.ssh \
    && chown deploy:deploy /home/deploy/.ssh
COPY probe.py /probe.py
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
