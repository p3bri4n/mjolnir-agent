FROM python:3.12-slim AS generator
WORKDIR /gen
RUN pip install --no-cache-dir Pillow==10.4.0
COPY generate_visual_probe.py .
RUN python3 generate_visual_probe.py /site/visual-probe

FROM nginx:alpine
COPY --from=generator /site /usr/share/nginx/html
