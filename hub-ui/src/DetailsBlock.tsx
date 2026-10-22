/** Context text, links and image attachments for a decision or task. Data only. */
import { useEffect, useRef, useState, type PointerEvent, type WheelEvent } from "react";
import { HubApiError } from "./api";
import { attachmentUrl, listAttachments, parseContext, type DetailImage } from "./details";
import { Markdown } from "./markdown";
import { link } from "./router";
import { Modal } from "./ui";

const external = (href: string) => !href.startsWith("/");

export function ImageViewer({ image, onClose }: { image: DetailImage; onClose: () => void }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const setScale = (value: number) => {
    const next = Math.min(8, Math.max(1, value));
    setZoom(next);
    if (next === 1) setOffset({ x: 0, y: 0 });
  };
  return (
    <Modal title={image.title} onClose={onClose} size="wide">
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
      <p className="panel-footnote">{(image.bytes / 1024).toFixed(0)} KB · Scroll or use + and − to zoom; drag to move.</p>
    </Modal>
  );
}

/**
 * Reads an entity's image attachments. `load` gates the read (decision cards load at once;
 * task rows when expanded). Demo mode never reads. Only a real 401/403 clears the workspace.
 */
export function useAttachments(kind: "task" | "decision", entity: { id: string; revision: number }, load: boolean,
  onUnauthorized: () => void) {
  const [images, setImages] = useState<DetailImage[]>([]);
  const [failed, setFailed] = useState(false);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  useEffect(() => {
    if (!load) return;
    const controller = new AbortController();
    setFailed(false);
    listAttachments(kind, entity.id, controller.signal).then((value) => {
      if (!controller.signal.aborted) setImages(value ?? []);
    }).catch((error) => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && [401, 403].includes(error.status)) unauthorized.current();
      else setFailed(true);
    });
    return () => controller.abort();
  }, [load, kind, entity.id, entity.revision]);
  return { images, failed };
}

export function DetailLinks({ links }: { links: { label: string; href: string }[] }) {
  if (links.length === 0) return null;
  return (
    <ul className="detail-links" aria-label="Links">
      {links.map((item) => (
        <li key={item.href + item.label}>
          <a href={external(item.href) ? item.href : link(item.href)}
            {...(external(item.href) ? { target: "_blank", rel: "noopener noreferrer" } : {})}>
            {item.label}
          </a>
          {external(item.href) && <small>{new URL(item.href).hostname}</small>}
        </li>
      ))}
    </ul>
  );
}

/**
 * Context text, links and image thumbnails. With `onOpenImage`, a thumbnail opens the caller's
 * view (the full question); without it, the image opens in the zoom viewer.
 */
export function DetailsBlock({ entity, kind, load, onUnauthorized, onOpenImage }: {
  entity: { id: string; revision: number }; kind: "task" | "decision"; load: boolean; onUnauthorized: () => void;
  onOpenImage?: (image: DetailImage) => void;
}) {
  const context = parseContext(entity);
  const { images, failed } = useAttachments(kind, entity, load, onUnauthorized);
  const [open, setOpen] = useState<DetailImage | null>(null);
  if (!context && images.length === 0 && !failed) return null;
  return (
    <div className="item-details">
      {context?.summary.trim() && <div className="item-summary"><Markdown source={context.summary} /></div>}
      {images.length > 0 && (
        <ul className="attachment-grid" aria-label="Attached images">
          {images.map((image) => (
            <li key={image.id}>
              <button type="button" className="attachment-thumb" onClick={() => (onOpenImage ?? setOpen)(image)}
                aria-label={`View ${image.title}`}>
                <img src={attachmentUrl(image.id)} alt="" loading="lazy" />
                <span>{image.title}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {failed && <p className="panel-footnote">Attached images could not be loaded right now.</p>}
      {context && <DetailLinks links={context.links} />}
      {open && <ImageViewer image={open} onClose={() => setOpen(null)} />}
    </div>
  );
}
