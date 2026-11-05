// CV styled after Adelchi's own LaTeX CV: navy sans-serif headings, serif body, shaded skills
// grid and a dates column. Still one reading order and real text, so ATS parsers read it.
// Fonts fall back to bundled ones when Avenir Next / Times New Roman are not installed.
#let cv = json("cv.json")
#let L = cv.labels
#let accent = rgb("#1d5b8a")
#let muted = rgb("#6b6b6b")
#let shade = rgb("#eef3f8")
#let heading-font = ("Avenir Next", "Avenir", "Helvetica Neue", "Libertinus Sans", "New Computer Modern Sans")
#let body-font = ("Times New Roman", "Times", "Libertinus Serif", "New Computer Modern")

#set document(title: cv.name + " — CV", author: cv.name)
#set page(
  paper: "a4",
  margin: (x: 1.5cm, top: 1.3cm, bottom: 1.5cm),
  footer: context align(right, text(size: 8pt, fill: muted, cv.name + "  |  " + counter(page).display())),
)
#set text(font: body-font, size: 10pt, lang: cv.lang)
#set par(justify: true, leading: 0.5em)
#set list(indent: 0.4em, body-indent: 0.5em, marker: text(fill: accent, "▪"))
#show link: set text(fill: accent)

#let section(title) = {
  v(0.7em)
  text(font: heading-font, size: 14pt, weight: "bold", fill: accent, title)
  v(-0.55em)
  line(length: 100%, stroke: 0.6pt + accent)
  v(0.1em)
}

// Header
#text(font: heading-font, size: 26pt, weight: "bold", fill: accent, cv.name)
#v(-0.7em)
#line(length: 100%, stroke: 0.6pt + accent)
#v(-0.3em)
#text(size: 11.5pt, style: "italic", fill: muted, cv.headline.replace(" | ", "  |  ")) \
#let c = cv.contact
#let parts = ("location", "phone", "email", "linkedin", "github").filter(k => k in c and c.at(k) != "" and c.at(k) != none).map(k => {
  let v = c.at(k)
  if k == "email" { link("mailto:" + v, v) } else if k in ("linkedin", "github") { link("https://" + v.trim("https://", at: start), v) } else { text(fill: muted, v) }
})
#text(size: 9pt, parts.join(text(fill: muted, "  •  ")))

#section(L.summary)
#cv.summary

#section(L.skills)
#block(stroke: 0.4pt + rgb("#d5dde6"), radius: 2pt, clip: true, grid(
  columns: (1fr, 1fr),
  fill: (_, y) => if calc.even(y) { shade } else { white },
  inset: (x: 7pt, y: 6pt),
  ..cv.skills.map(s => [
    #text(font: body-font, weight: "bold", fill: accent, s.group) \
    #set par(justify: false)
    #text(size: 9.5pt, s.items.join(", "))
  ]),
))

#section(L.experience)
#for e in cv.experience [
  #v(0.35em)
  #grid(
    columns: (2.9cm, 1fr),
    column-gutter: 0.4cm,
    par(justify: false, text(size: 9pt, weight: "bold", fill: accent, e.dates)),
    [
      #text(size: 11pt, weight: "bold", e.role)#text(fill: muted, "  |  ")#text(size: 11pt, weight: "bold", fill: accent, e.company)
      #if e.location != "" [ #text(size: 9pt, fill: muted, "· " + e.location)]
      #if e.at("context", default: "") != "" [ \ #text(size: 9.5pt, style: "italic", fill: muted, e.context)]
      #for b in e.bullets [
        - #b
      ]
      #if e.at("technology", default: "") != "" [
        #text(size: 9pt)[#text(weight: "bold", fill: accent, L.at("technology", default: "Technology") + ":") #text(fill: muted, e.technology)]
      ]
    ],
  )
]

#if cv.education.len() > 0 [
  #section(L.education)
  #for ed in cv.education [
    #grid(
      columns: (2.9cm, 1fr),
      column-gutter: 0.4cm,
      row-gutter: 0.5em,
      text(size: 9pt, weight: "bold", fill: accent, ed.year),
      [*#ed.degree*#text(fill: muted, "  |  ")#text(fill: accent, ed.school)],
    )
  ]
]

#if cv.languages.len() > 0 [
  #section(L.languages)
  #cv.languages.join(text(fill: muted, "  •  "))
]

#for s in cv.at("sections", default: ()) [
  #section(s.title)
  #set par(justify: false)
  #for i in s.items [
    - #i
  ]
]
