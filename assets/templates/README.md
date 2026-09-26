# Drop PNG icons here to let the agent recognise controls that OCR cannot read
# (icon-only buttons, toolbar glyphs, app logos).
#
# Name the file after the control, e.g.
#   export_button.png
#   play_icon.png
#   capcut_logo.png
#
# The agent will then be able to target it as:  click text="export button"
# Matching is done with OpenCV template matching (threshold in
# config: perception.<...> / TemplateMatcher.threshold, default 0.82).
# Bigger crops match more reliably - 48x48 or larger is a good target.
