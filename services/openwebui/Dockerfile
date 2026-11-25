# Open WebUI with two small additions. Branding and licence files are untouched.
#  1. New browsers start in dark mode instead of following the system (users can still switch).
#  2. A stylesheet and one image for the sign-in page, loaded through Open WebUI's own
#     /static/custom.css hook.
FROM ghcr.io/open-webui/open-webui:v0.13.4
RUN sed -i "s/localStorage.theme = 'system';/localStorage.theme = 'dark';/" /app/build/index.html \
 && grep -q "localStorage.theme = 'dark';" /app/build/index.html
COPY theme/custom.css theme/stratum-log.svg /app/build/static/
