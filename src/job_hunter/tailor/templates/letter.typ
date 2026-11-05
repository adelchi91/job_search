// Plain cover letter. Data comes from letter.json written next to this file.
#let d = json("letter.json")
#set document(title: d.name + " — " + d.title, author: d.name)
#set page(paper: "a4", margin: 2.2cm)
#set text(font: ("Libertinus Serif", "New Computer Modern"), size: 11pt, lang: d.lang)
#set par(justify: true, leading: 0.65em, spacing: 1.1em)

#text(weight: "bold", d.name) \
#d.contact

#v(1.2em)
#d.company — #d.title
#v(1.2em)

#for p in d.paragraphs [
  #p

]
