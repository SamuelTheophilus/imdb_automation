"""First-time user intro tour using Driver.js."""

# ── Sample row shown in the review drawer during the tour ────────────────────
# Uses id=-1 and db_id=None so save/delete handlers are no-ops on it.
TOUR_SAMPLE_ROW: dict = {
    "id":           -1,
    "db_id":        None,
    "product_name": "Milo Chocolate Malt Beverage",
    "brand":        "Milo",
    "weight":       "400G",
    "category_type":  "BEVERAGE",
    "segment_type":   "CHOCOLATE DRINKS",
    "barcode":        "",
    "manufacturer":   "Nestlé Ghana Ltd.",
    "packaging_type": "TIN",
    "country_of_origin": "GHANA",
    "promotional_messages": "",
    "variant":        "ORIGINAL",
    "fragrance_flavor": "CHOCOLATE",
    "addons":         "",
    "tagline":        "Energy to Go!",
    "_status":        "ok",
    "image_path":     "",
    "image_paths":    [],
    "thumbnail":      "",
    "_normalized":    "",
    "_low":           "",
    "_is_tour":       True,
}

# ── Driver.js tour script ────────────────────────────────────────────────────
# Runs client-side after the page and drawer have settled.
TOUR_JS = """
(function () {
  if (!window.driver || !window.driver.js) return;

  const driverObj = window.driver.js.driver({
    showProgress: true,
    progressText: '{{current}} of {{total}}',
    nextBtnText:  'Next',
    prevBtnText:  'Back',
    doneBtnText:  'Done',
    popoverClass: 'imdb-tour-popover',
    onDestroyed: function () {
      // Close the review drawer by clicking its X button
      var icons = document.querySelectorAll('.q-drawer--right .material-icons');
      for (var i = 0; i < icons.length; i++) {
        if (icons[i].textContent.trim() === 'close') {
          var btn = icons[i].closest('button');
          if (btn) btn.click();
          break;
        }
      }
    },
    steps: [
      {
        element: '.upload-zone',
        popover: {
          title: 'Quick Upload',
          description:
            'Drop up to <strong>20 images</strong> here for instant extraction. ' +
            'Multiple angles of the same product are grouped automatically.',
          side: 'bottom',
          align: 'center',
        },
      },
      {
        element: '.bulk-batch-tab',
        popover: {
          title: 'Bulk Batch — process hundreds at once',
          description:
            'Switch to the <strong>Bulk Batch</strong> tab to submit any number of images. ' +
            'They are processed overnight via the Anthropic Batch API (50% cost) ' +
            'and results are emailed to you when ready — no need to stay on the page.',
          side: 'bottom',
          align: 'start',
        },
      },
      {
        element: '.ag-root-wrapper',
        popover: {
          title: 'Extracted products',
          description:
            'After processing, each product group appears as a row here. ' +
            'Sort, filter, and inline-edit any field. ' +
            'Click <strong>Review</strong> on any row to open the side panel.',
          side: 'top',
          align: 'center',
        },
      },
      {
        element: '.q-drawer--right',
        popover: {
          title: 'Review panel',
          description:
            'Check and correct extracted fields here. Mark a product as ' +
            '<strong>OK</strong> when you are happy with the data. ' +
            'Click <strong>Done</strong> to finish the tour.',
          side: 'left',
          align: 'center',
        },
      },
    ],
  });

  driverObj.drive();
})();
"""
