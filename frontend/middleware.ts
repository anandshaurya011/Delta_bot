import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

const isPublic = createRouteMatcher(["/sign-in(.*)"]);
const isApi = createRouteMatcher(["/api(.*)"]);

export default clerkMiddleware(async (auth, req) => {
  // /api/* routes gate their own auth (see app/api/[...path]/route.ts), returning a
  // clean 401 JSON body when signed out. clerkMiddleware still needs to run on these
  // requests so that auth()/getToken() work inside the route handler, but it must not
  // call auth.protect() itself or it short-circuits with a 404/307 before the handler
  // ever runs.
  if (!isPublic(req) && !isApi(req)) await auth.protect();
});

export const config = { matcher: ["/((?!_next|.*\\..*).*)", "/(api)(.*)"] };
