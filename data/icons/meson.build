application_id = 'io.github.drvonmiau.Lyre'

# App icon (512x512): the muse logo tile, cut out with transparent rounded
# corners. The uncut master artwork is kept outside the repo.
png_dir = 'hicolor' / '512x512' / 'apps'
install_data(
  png_dir / ('@0@.png').format(application_id),
  install_dir: get_option('datadir') / 'icons' / png_dir
)

symbolic_dir = 'hicolor' / 'symbolic' / 'apps'
install_data(
  symbolic_dir / ('@0@-symbolic.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / symbolic_dir
)
