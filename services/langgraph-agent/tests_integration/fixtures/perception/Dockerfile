FROM python:3.12-slim AS generator
WORKDIR /gen
RUN pip install --no-cache-dir Pillow==10.4.0
COPY generate_perception.py .
RUN python3 generate_perception.py /site/perception

FROM nginx:alpine
COPY --from=generator /site /usr/share/nginx/html
