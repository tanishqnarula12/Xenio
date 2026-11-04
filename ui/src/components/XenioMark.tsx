/**
 * XENIO's logo: one X, one colour. A heavy stroke cut as a broad pen leaves it, and two thin blades.
 * Small sizes use a heavier cut of the same X, so the blades survive. It takes its colour from the text around it.
 * The same drawing is in docs/assets/xenio-logo.svg, ui/public/favicon.svg and native/orb/make_icon.swift.
 */
const LARGE = "M112 40L222 74L400 472L290 438ZM412 30L286.4 193.5L296.7 216.6ZM100 482L225.6 318.5L215.3 295.4Z";
const SMALL = "M84 30L248 76L428 482L264 436ZM448 20L306.9 159.5L327.5 206ZM64 492L205.1 352.5L184.5 306Z";

export function XenioMark({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 512 512" fill="currentColor" aria-hidden="true" focusable="false">
      <path d={size < 44 ? SMALL : LARGE} />
    </svg>
  );
}
