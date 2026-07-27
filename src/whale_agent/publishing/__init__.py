"""Publishing: a thesis rendered as a page a reader can be sent a link to.

Separate from `summarization/` because the two answer different questions. Summarization
decides what may be said; publishing decides what it looks like once it may be said. The
split keeps the provenance gate upstream of every renderer, so a new output format cannot
accidentally become a new way to print an unsourced figure.
"""
