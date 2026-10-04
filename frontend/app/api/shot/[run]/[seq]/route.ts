import { readScreenshot } from "@/lib/bundle";

// Serves a decision's screenshot from the local bundle (never copied into public/).
export async function GET(_req: Request, ctx: RouteContext<"/api/shot/[run]/[seq]">) {
  const { run, seq } = await ctx.params;
  const image = await readScreenshot(decodeURIComponent(run), Number(seq));
  if (!image) return new Response("not found", { status: 404 });
  return new Response(image, { headers: { "Content-Type": "image/bmp", "Cache-Control": "no-store" } });
}
