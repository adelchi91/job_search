// Cover letter in the same look as cv.typ. Data comes from letter.json written next to this file.
#let d = json("letter.json")
#let accent = rgb("#1d5b8a")
#let muted = rgb("#6b6b6b")
#let heading-font = ("Avenir Next", "Avenir", "Helvetica Neue", "Libertinus Sans", "New Computer Modern Sans")
#set document(title: d.name + " — " + d.title, author: d.name)
#set page(paper: "a4", margin: 2.2cm)
#set text(font: ("Times New Roman", "Times", "Libertinus Serif", "New Computer Modern"), size: 11pt, lang: d.lang)
#set par(justify: true, leading: 0.65em, spacing: 1.1em)

#text(font: heading-font, size: 20pt, weight: "bold", fill: accent, d.name)
#v(-0.6em)
#line(length: 100%, stroke: 0.6pt + accent)
#v(-0.4em)
#text(size: 9.5pt, fill: muted, d.contact)

#v(1.2em)
#text(weight: "bold", fill: accent, d.company) — #text(style: "italic", d.title)
#v(1.2em)

#for p in d.paragraphs [
  #p

]
