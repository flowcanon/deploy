FROM nginx:alpine
COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/
COPY README.md /usr/share/nginx/html/
COPY SPEC.md /usr/share/nginx/html/
COPY docs/ /usr/share/nginx/html/docs/
COPY script/install /usr/share/nginx/html/install
EXPOSE 80
