FROM nginx:alpine
COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/
COPY README.md /usr/share/nginx/html/
COPY SPEC.md /usr/share/nginx/html/
COPY CHANGELOG.md /usr/share/nginx/html/
COPY docs/ /usr/share/nginx/html/docs/
COPY script/install /usr/share/nginx/html/install
COPY pyproject.toml /tmp/pyproject.toml
RUN VERSION=$(grep '^version' /tmp/pyproject.toml | sed 's/.*"\(.*\)"/\1/') && \
    sed -i "s/__VERSION__/v${VERSION}/g" /usr/share/nginx/html/*.html && \
    rm /tmp/pyproject.toml
EXPOSE 80
