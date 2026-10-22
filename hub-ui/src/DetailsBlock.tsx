/** Summary, links and image attachments for a decision or task. Data only. */
import { useRef, useState, type PointerEvent, type WheelEvent } from "react";
import { attachmentUrl, parseDetails, type DetailImage } from "./details";
import { Markdown } from "./markdown";
import { link } from "./router";
import { Modal } from "./ui";

const external = (href: string) => !href.startsWith("/");

function ImageViewer({ image, onClose }: { image: DetailImage; onClose: () => void }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const clamp = (value: number) => Math.min(8, Math.max(1, value));
  const setScale = (value: number) => {
    const next = clamp(value);
    setZoom(next);
    if (next === 1) setOffset({ x: 0, y: 0 });
  };
  return (
    <Modal title={image.title} onClose={onClose}>
      <div className="image-viewer-tools" role="group" aria-label="Zoom">
        <button type="button" className="secondary small" onClick={() => setScale(zoom / 1.5)} disabled={zoom <= 1}>−</button>
        <span aria-live="polite">{Math.round(zoom * 100)}%</span>
        <button type="button" className="secondary small" onClick={() => setScale(zoom * 1.5)} disabled={zoom >= 8}>+</button>
        <button type="button" className="secondary small" onClick={() => setScale(1)}>Fit</button>
        <a className="secondary small button-link" href={attachmentUrl(image.id)} target="_blank" rel="noopener noreferrer">Open original</a>
      </div>
      <div className="image-viewer"
        onWheel={(event: WheelEvent) => { event.preventDefault(); setScale(zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15)); }}
        onPointerDown={(event: PointerEvent) => {
          if (zoom === 1) return;
          drag.current = { x: event.clientX - offset.x, y: event.clientY - offset.y };
          (event.currentTarget as Element).setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event: PointerEvent) => {
          if (drag.current) setOffset({ x: event.clientX - drag.current.x, y: event.clientY - drag.current.y });
        }}
        onPointerUp={() => { drag.current = null; }}>
        <img src={attachmentUrl(image.id)} alt={image.title} draggable={false}
          style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }} />
      </div>
      <p className="panel-footnote">{image.width} × {image.height} px · {(image.bytes / 1024 / 1024).toFixed(1)} MB.
        Scroll or use + and − to zoom; drag to move.</p>
    </Modal>
  );
}

export function DetailsBlock({ value }: { value: unknown }) {
  const details = parseDetails(value);
  const [open, setOpen] = useState<DetailImage | null>(null);
  if (!details) return null;
  return (
    <div className="item-details">
      {details.summary.trim() && <div className="item-summary"><Markdown source={details.summary} /></div>}
      {details.images.length > 0 && (
        <ul className="attachment-grid" aria-label="Attached images">
          {details.images.map((image) => (
            <li key={image.id}>
              <button type="button" className="attachment-thumb" onClick={() => setOpen(image)}
                aria-label={`View ${image.title}`}>
                <img src={attachmentUrl(image.id)} alt="" loading="lazy" width={image.width} height={image.height} />
                <span>{image.title}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {details.links.length > 0 && (
        <ul className="detail-links" aria-label="Links">
          {details.links.map((item) => (
            <li key={item.href + item.label}>
              <a href={external(item.href) ? item.href : link(item.href)} {...(external(item.href) ? { target: "_blank", rel: "noopener noreferrer" } : {})}>
                {item.label}
              </a>
              {external(item.href) && <small>{new URL(item.href).hostname}</small>}
            </li>
          ))}
        </ul>
      )}
      {open && <ImageViewer image={open} onClose={() => setOpen(null)} />}
    </div>
  );
}
