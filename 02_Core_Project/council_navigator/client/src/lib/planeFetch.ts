import { isPublicPlane } from "./trustPlane";

export type PlanePaths = Readonly<{
  publicPath: string;
  operatorPath: string;
}>;

/** Select the data source for the renderer's current trust plane. */
export function planePath({ publicPath, operatorPath }: PlanePaths): string {
  return isPublicPlane() ? publicPath : operatorPath;
}

/** Fetch the public DTO on the public plane and preserve the operator path elsewhere. */
export function fetchForPlane(
  paths: PlanePaths,
  init?: RequestInit,
): Promise<Response> {
  return fetch(planePath(paths), init);
}
