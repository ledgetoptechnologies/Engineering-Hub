import { describe, expect, it } from "vitest";
import { galleryColumns } from "./pages/decisions";

describe("galleryColumns", () => {
  it("shows one image large, two side by side, four as 2 x 2 and larger sets three across", () => {
    expect([1, 2, 3, 4, 5, 6, 9].map(galleryColumns)).toEqual([1, 2, 3, 2, 3, 3, 3]);
  });
});
