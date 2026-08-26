import React from "react";
import { Composition } from "remotion";

import { Hero, HeroProps } from "./Hero";
import { DURATION, FPS, HEIGHT, WIDTH } from "./theme";

/**
 * One composition, parameterised by release. A new release renders the same
 * `Hero` with new props; the motif module is what a new codename replaces.
 */
export const RemotionRoot: React.FC = () => {
  return (
    <>
    <Composition
      id="Hero"
      component={Hero}
      durationInFrames={DURATION}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
      defaultProps={
        {
          version: "2.6.0",
          codename: "Clockwork",
          seed: 260,
        } satisfies HeroProps
      }
    />
    {/* One frame longer than the loop, so frame DURATION can be rendered and
        diffed against frame 0 to prove the loop is seamless. Never rendered as
        a deliverable. */}
    <Composition
      id="LoopCheck"
      component={Hero}
      durationInFrames={DURATION + 1}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
      defaultProps={
        {
          version: "2.6.0",
          codename: "Clockwork",
          seed: 260,
          loopFrames: DURATION,
        } satisfies HeroProps
      }
    />
    </>
  );
};
