FROM nginx:alpine
COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/
COPY SPEC.md /usr/share/nginx/html/
EXPOSE 80
