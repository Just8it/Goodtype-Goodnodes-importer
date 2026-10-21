Editable shapes and smaller vector fallback objects (0.1.1).

- Converts supported shapes to ordinary editable Goodtype lines, rectangles, ellipses and splines.
- Retains curve handles and rotated ellipse centres without fitting handwriting into shapes.
- Crops fallback SVGs to their geometry; merges nearby marks into bounded groups while preserving
  overlapping drawing order. Distant artwork and cropped images remain independently selectable.
- Reports editable shape counts and fallback placement, with complete decoded-object accounting.
- Keeps the existing offline bundle, CLI protocol, notebook schema, source preservation and opt-in.

The decoder remains experimental. Review converted pages and keep separate original exports.
Text/sticky notes, audio, PDF stickers and partially erased ink are not supported. The package is
unsigned; Windows may show an unrecognized-app warning. Custom shape dashes, arrows, translucent
fills and unrepresentable paths remain artwork or fail explicitly. Existing notebooks need a
fresh import to receive these changes; the Goodtype app must pin this tested release separately.
