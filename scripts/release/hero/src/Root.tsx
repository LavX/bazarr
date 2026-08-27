import React from "react";
import { Composition } from "remotion";

import { Hero, HeroProps } from "./Hero";
import { SiteHero, SiteHeroProps } from "./SiteHero";
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
    {/* The public site's hero background. It carries no type: the page draws
        its own headline over this in real, selectable text. */}
    <Composition
      id="SiteHero"
      component={SiteHero}
      durationInFrames={DURATION}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
      defaultProps={{ seed: 826 } satisfies SiteHeroProps}
    />
    <Composition
      id="SiteHeroLoopCheck"
      component={SiteHero}
      durationInFrames={DURATION + 1}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
      defaultProps={{ seed: 826, loopFrames: DURATION } satisfies SiteHeroProps}
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
