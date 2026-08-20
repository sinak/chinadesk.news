# static/

Copied verbatim into `dist/static/` at build time.

## og.png

The unfurl image for links to the site. `build.og_image()` reads its real
width and height out of the PNG header and the template emits them alongside
the URL; if the file is absent no `og:image` tag is written at all and the
Twitter card degrades to `summary`, because an unfurl pointing at a 404 reads
worse than one with no image and platforms cache the result for days.

Replacing it is enough — nothing needs updating elsewhere. Keep it PNG, at
least 1200x630, and ideally under about 1MB: several platforms refuse to fetch
larger files and simply show no card.
