'use strict'

// Extend page-pagination attribute from Antora Default UI to support "sibling" value

// "", "prev", and "next" values have the same meaning as the Antora Default UI
// https://docs.antora.org/antora-ui-default/template-customization/#page-pagination-attribute
// "prev" shows the previous reachable page in the navigation tree (skips past text-only and external items).
// "next" shows the next reachable page in the navigation tree (skips past text-only and external items).
// "" shows both the previous & next pages

// "sibling" shows
// - the previous page at the same navigation level, if a page exists within the same parent
// - the next page at the same navigation level, if a page exists within the same parent and if the
//   current page has no children (i.e. no pages at a nested navigation level)

// This UI helper is only intended to be used by partials/pagination.hbs

module.exports = ({data: {root}}) => {
    const {page} = root
    const {pagination} = page.attributes
    if (!["", "prev", "next", "sibling"].includes(pagination)) {
        throw new Error("page-pagination attribute must be '', 'prev', 'next', or 'sibling', got '" + pagination + "'")
    }
    let previous = null
    let next = null
    if (["", "prev"].includes(pagination) && page.previous) {
        previous = page.previous
    }
    if (["", "next"].includes(pagination) && page.next) {
        next = page.next
    }
    if (pagination === "sibling") {
        if (page.parent === undefined) {
            throw new Error("page-pagination 'sibling' not implemented for pages in top-level of navigation")
        }
        const siblings = page.parent.items
        const this_page_index = siblings.findIndex(page_ => page_.url === page.url)
        previous = siblings[this_page_index-1] || null
        const this_page_has_no_children = siblings[this_page_index].items === undefined
        if (this_page_has_no_children) {
            next = siblings[this_page_index+1] || null
        }
    }
    return {previous: previous, next: next}
}
