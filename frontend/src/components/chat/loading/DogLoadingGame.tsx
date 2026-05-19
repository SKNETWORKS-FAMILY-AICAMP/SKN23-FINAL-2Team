import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, RefObject } from "react";
import { ChevronDown, ChevronUp, Volume2, VolumeX } from "lucide-react";
import dogRunSrc from "../../../assets/sprites/run.png";
import dogDieSrc from "../../../assets/sprites/die.png";
import jumpSoundSrc from "../../../assets/sprites/jump.mp3";
import boneSoundSrc from "../../../assets/sprites/bone.mp3";

type DogLoadingGameProps = {
  active: boolean;
  title?: string;
  subtitle?: string;
};

type Obstacle = {
  x: number;
  height: number;
  width: number;
};

type Bone = {
  x: number;
  y: number;
  width: number;
  height: number;
  collected: boolean;
};

const DEFAULT_GAME_WIDTH = 400;
const MIN_GAME_WIDTH = 260;
const GAME_HEIGHT = 180;
const GROUND_Y = 132;

const DOG_X = 70;
const DOG_RENDER_WIDTH = 64;
const DOG_RENDER_HEIGHT = 64;
const DOG_GROUND_OFFSET = -7;

const SPRITE_FRAME_WIDTH = 32;
const SPRITE_FRAME_HEIGHT = 32;
const RUN_FRAME_COUNT = 4;
const DIE_FRAME_COUNT = 2;

const OBSTACLE_WIDTH = 24;

export default function DogLoadingGame({
  active,
  title = "도면을 분석하는 중입니다...",
  subtitle = "Space로 점프하고 뼈다귀를 먹어 점수를 얻을 수 있습니다.",
}: DogLoadingGameProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  const runImageRef = useRef<HTMLImageElement | null>(null);
  const dieImageRef = useRef<HTMLImageElement | null>(null);
  const jumpAudioRef = useRef<HTMLAudioElement | null>(null);
  const boneAudioRef = useRef<HTMLAudioElement | null>(null);

  const frameRef = useRef<number | null>(null);
  const resizeTimerRef = useRef<number | null>(null);
  const lastTimeRef = useRef(0);

  const [gameWidth, setGameWidth] = useState(DEFAULT_GAME_WIDTH);
  const [score, setScore] = useState(0);
  const [gameOver, setGameOver] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [soundEnabled, setSoundEnabled] = useState(true);

  const gameWidthRef = useRef(DEFAULT_GAME_WIDTH);
  const dogYRef = useRef(GROUND_Y - DOG_RENDER_HEIGHT - DOG_GROUND_OFFSET);
  const velocityYRef = useRef(0);
  const isJumpingRef = useRef(false);
  const soundEnabledRef = useRef(true);
  const gameOverRef = useRef(false);

  const obstaclesRef = useRef<Obstacle[]>([
    { x: DEFAULT_GAME_WIDTH + 120, height: 42, width: OBSTACLE_WIDTH },
  ]);
  const bonesRef = useRef<Bone[]>([]);
  const speedRef = useRef(300);
  const scoreRef = useRef(0);

  const getDogGroundY = () => GROUND_Y - DOG_RENDER_HEIGHT - DOG_GROUND_OFFSET;

  const makeObstacles = useCallback((startX: number): Obstacle[] => {
    const height = Math.random() > 0.5 ? 42 : 54;
    return [{ x: startX, height, width: OBSTACLE_WIDTH }];
  }, []);

  const makeBones = useCallback((startX: number): Bone[] => {
    if (Math.random() < 0.45) return [];

    return [
      {
        x: startX + 70 + Math.random() * 80,
        y: GROUND_Y - 86,
        width: 28,
        height: 16,
        collected: false,
      },
    ];
  }, []);

  const resetGame = useCallback(() => {
    dogYRef.current = getDogGroundY();
    velocityYRef.current = 0;
    isJumpingRef.current = false;

    const startX = gameWidthRef.current + 120;
    obstaclesRef.current = makeObstacles(startX);
    bonesRef.current = makeBones(startX);

    speedRef.current = 300;
    scoreRef.current = 0;

    setScore(0);
    setGameOver(false);
    gameOverRef.current = false;
  }, [makeObstacles, makeBones]);

  const playSound = (audioRef: RefObject<HTMLAudioElement | null>) => {
    const audio = audioRef.current;
    if (!audio || !soundEnabledRef.current) return;

    audio.currentTime = 0;
    audio.play().catch(() => {});
  };

  const jump = useCallback(() => {
    if (!active || collapsed) return;

    if (gameOverRef.current) {
      resetGame();
      return;
    }

    if (!isJumpingRef.current) {
      velocityYRef.current = -520;
      isJumpingRef.current = true;
      playSound(jumpAudioRef);
    }
  }, [active, collapsed, resetGame]);

  const drawDog = (
    ctx: CanvasRenderingContext2D,
    x: number,
    y: number,
    time: number
  ) => {
    ctx.imageSmoothingEnabled = false;

    if (gameOverRef.current) {
      const image = dieImageRef.current;
      if (!image || !image.complete || image.naturalWidth === 0) return;

      const frame = Math.floor(time / 180) % DIE_FRAME_COUNT;

      ctx.drawImage(
        image,
        frame * SPRITE_FRAME_WIDTH,
        0,
        SPRITE_FRAME_WIDTH,
        SPRITE_FRAME_HEIGHT,
        x,
        y,
        DOG_RENDER_WIDTH,
        DOG_RENDER_HEIGHT
      );
      return;
    }

    const image = runImageRef.current;
    if (!image || !image.complete || image.naturalWidth === 0) return;

    const frame = Math.floor(time / 90) % RUN_FRAME_COUNT;

    ctx.drawImage(
      image,
      frame * SPRITE_FRAME_WIDTH,
      0,
      SPRITE_FRAME_WIDTH,
      SPRITE_FRAME_HEIGHT,
      x,
      y,
      DOG_RENDER_WIDTH,
      DOG_RENDER_HEIGHT
    );
  };

  const drawObstacle = (
    ctx: CanvasRenderingContext2D,
    x: number,
    height: number
  ) => {
    const y = GROUND_Y - height;

    ctx.save();
    ctx.fillStyle = "#9ca3af";
    ctx.fillRect(x + 8, y, 8, height);
    ctx.fillRect(x + 4, y + 10, 16, 8);
    ctx.fillRect(x + 2, y + 20, 5, 12);
    ctx.fillRect(x + 17, y + 20, 5, 12);
    ctx.fillRect(x + 5, y + height - 5, 14, 5);
    ctx.restore();
  };

  const drawBone = (ctx: CanvasRenderingContext2D, bone: Bone) => {
    if (bone.collected) return;

    const { x, y } = bone;

    ctx.save();
    ctx.fillStyle = "#f8fafc";

    ctx.fillRect(x + 7, y + 6, 14, 5);

    ctx.beginPath();
    ctx.arc(x + 5, y + 5, 4, 0, Math.PI * 2);
    ctx.arc(x + 5, y + 13, 4, 0, Math.PI * 2);
    ctx.arc(x + 23, y + 5, 4, 0, Math.PI * 2);
    ctx.arc(x + 23, y + 13, 4, 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();
  };

  const drawCloud = (ctx: CanvasRenderingContext2D, x: number, y: number) => {
    ctx.save();
    ctx.strokeStyle = "rgba(156, 163, 175, 0.55)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, y + 16);
    ctx.lineTo(x + 20, y + 16);
    ctx.quadraticCurveTo(x + 25, y, x + 42, y + 8);
    ctx.quadraticCurveTo(x + 58, y + 2, x + 68, y + 16);
    ctx.lineTo(x + 92, y + 16);
    ctx.stroke();
    ctx.restore();
  };

  const drawGround = (
    ctx: CanvasRenderingContext2D,
    time: number,
    width: number
  ) => {
    ctx.save();
    ctx.strokeStyle = "rgba(209, 213, 219, 0.75)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(0, GROUND_Y);
    ctx.lineTo(width, GROUND_Y);
    ctx.stroke();

    ctx.fillStyle = "rgba(209, 213, 219, 0.55)";
    const offset = (time / 20) % 28;

    for (let i = -offset; i < width; i += 28) {
      ctx.fillRect(i, GROUND_Y + 13, 4, 3);
    }

    ctx.restore();
  };

  const getDogHitBox = () => ({
    left: DOG_X + 18,
    right: DOG_X + DOG_RENDER_WIDTH - 12,
    top: dogYRef.current + 20,
    bottom: dogYRef.current + DOG_RENDER_HEIGHT - 8,
  });

  const checkObstacleCollision = () => {
    const dog = getDogHitBox();

    return obstaclesRef.current.some((obstacle) => {
      const left = obstacle.x;
      const right = obstacle.x + obstacle.width;
      const top = GROUND_Y - obstacle.height;
      const bottom = GROUND_Y;

      return (
        dog.right > left &&
        dog.left < right &&
        dog.bottom > top &&
        dog.top < bottom
      );
    });
  };

  const checkBoneCollection = () => {
    const dog = getDogHitBox();

    bonesRef.current.forEach((bone) => {
      if (bone.collected) return;

      const left = bone.x;
      const right = bone.x + bone.width;
      const top = bone.y;
      const bottom = bone.y + bone.height;

      const hit =
        dog.right > left &&
        dog.left < right &&
        dog.bottom > top &&
        dog.top < bottom;

      if (hit) {
        bone.collected = true;
        scoreRef.current += 3;
        setScore(scoreRef.current);
        playSound(boneAudioRef);
      }
    });
  };

  const render = useCallback(
    (time: number) => {
      const canvas = canvasRef.current;
      const ctx = canvas?.getContext("2d");
      const width = gameWidthRef.current;

      if (!canvas || !ctx || !active || collapsed) return;

      if (!lastTimeRef.current) lastTimeRef.current = time;

      const delta = Math.min((time - lastTimeRef.current) / 1000, 0.033);
      lastTimeRef.current = time;

      ctx.clearRect(0, 0, width, GAME_HEIGHT);

      ctx.fillStyle = "rgba(209, 213, 219, 0.5)";
      ctx.fillRect(width * 0.14, 28, 3, 3);
      ctx.fillRect(width * 0.36, 55, 3, 3);
      ctx.fillRect(width * 0.54, 24, 3, 3);
      ctx.fillRect(width * 0.7, 50, 3, 3);
      ctx.fillRect(width * 0.9, 34, 3, 3);

      drawCloud(ctx, Math.max(width - 120, 10), 36);
      drawGround(ctx, time, width);

      if (!gameOverRef.current) {
        velocityYRef.current += 1450 * delta;
        dogYRef.current += velocityYRef.current * delta;

        if (dogYRef.current >= getDogGroundY()) {
          dogYRef.current = getDogGroundY();
          velocityYRef.current = 0;
          isJumpingRef.current = false;
        }

        obstaclesRef.current = obstaclesRef.current.map((obstacle) => ({
          ...obstacle,
          x: obstacle.x - speedRef.current * delta,
        }));

        bonesRef.current = bonesRef.current.map((bone) => ({
          ...bone,
          x: bone.x - speedRef.current * delta,
        }));

        const allPassed = obstaclesRef.current.every(
          (obstacle) => obstacle.x < -obstacle.width
        );

        if (allPassed) {
          const startX = width + 120 + Math.random() * 180;
          obstaclesRef.current = makeObstacles(startX);
          bonesRef.current = makeBones(startX);

          scoreRef.current += 1;
          speedRef.current = Math.min(speedRef.current + 8, 520);
          setScore(scoreRef.current);
        }

        checkBoneCollection();

        if (checkObstacleCollision()) {
          gameOverRef.current = true;
          setGameOver(true);
        }
      }

      bonesRef.current.forEach((bone) => drawBone(ctx, bone));
      drawDog(ctx, DOG_X, dogYRef.current, time);
      obstaclesRef.current.forEach((obstacle) => {
        drawObstacle(ctx, obstacle.x, obstacle.height);
      });

      ctx.fillStyle = "rgba(229, 231, 235, 0.9)";
      ctx.font = "14px monospace";
      ctx.fillText(
        `SCORE ${scoreRef.current}`,
        Math.max(width - 110, DOG_X + DOG_RENDER_WIDTH + 8),
        20
      );

      if (gameOverRef.current) {
        ctx.fillStyle = "rgba(0, 0, 0, 0.45)";
        ctx.fillRect(0, 0, width, GAME_HEIGHT);

        ctx.fillStyle = "#e5e7eb";
        ctx.font = "20px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("충돌했습니다", width / 2, GAME_HEIGHT * 0.42);

        ctx.font = "14px sans-serif";
        ctx.fillStyle = "#9ca3af";
        ctx.fillText(
          "Space를 누르면 다시 시작합니다",
          width / 2,
          GAME_HEIGHT * 0.62
        );
        ctx.textAlign = "start";
      }
    },
    [active, collapsed, makeObstacles, makeBones]
  );

  useEffect(() => {
    const updateSize = () => {
      const wrapper = wrapperRef.current;

      const rawWidth =
        wrapper?.clientWidth ||
        wrapper?.parentElement?.clientWidth ||
        DEFAULT_GAME_WIDTH;

      const nextWidth = Math.max(MIN_GAME_WIDTH, Math.floor(rawWidth - 24));

      gameWidthRef.current = nextWidth;
      setGameWidth(nextWidth);
    };

    updateSize();

    let resizeObserver: ResizeObserver | null = null;

    if (typeof ResizeObserver !== "undefined" && wrapperRef.current) {
      resizeObserver = new ResizeObserver(updateSize);
      resizeObserver.observe(wrapperRef.current);
    } else {
      resizeTimerRef.current = window.setInterval(updateSize, 250);
    }

    window.addEventListener("resize", updateSize);

    return () => {
      resizeObserver?.disconnect();

      if (resizeTimerRef.current != null) {
        window.clearInterval(resizeTimerRef.current);
        resizeTimerRef.current = null;
      }

      window.removeEventListener("resize", updateSize);
    };
  }, []);

  useEffect(() => {
    const runImage = new Image();
    runImage.src = dogRunSrc;
    runImageRef.current = runImage;

    const dieImage = new Image();
    dieImage.src = dogDieSrc;
    dieImageRef.current = dieImage;

    const jumpAudio = new Audio(jumpSoundSrc);
    jumpAudio.volume = 0.25;
    jumpAudio.preload = "auto";
    jumpAudioRef.current = jumpAudio;

    const boneAudio = new Audio(boneSoundSrc);
    boneAudio.volume = 0.28;
    boneAudio.preload = "auto";
    boneAudioRef.current = boneAudio;
  }, []);

  useEffect(() => {
    soundEnabledRef.current = soundEnabled;

    if (!soundEnabled) {
      jumpAudioRef.current?.pause();
      boneAudioRef.current?.pause();
    }
  }, [soundEnabled]);

  useEffect(() => {
    if (!active) {
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }

      lastTimeRef.current = 0;
      resetGame();
      return;
    }

    if (collapsed) {
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }

      lastTimeRef.current = 0;
      return;
    }

    wrapperRef.current?.focus();

    const tick = (time: number) => {
      render(time);
      frameRef.current = window.requestAnimationFrame(tick);
    };

    frameRef.current = window.requestAnimationFrame(tick);

    return () => {
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }

      lastTimeRef.current = 0;
    };
  }, [active, collapsed, render, resetGame]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.code === "Space" || event.code === "ArrowUp") {
      event.preventDefault();
      jump();
    }
  };

  const stopControlKeyPropagation = (event: KeyboardEvent<HTMLButtonElement>) => {
    event.stopPropagation();
  };

  if (!active) return null;

  return (
    <div
      ref={wrapperRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      className="mx-auto mt-3 flex w-full flex-col items-center px-1 outline-none"
    >
      <div className="relative w-full overflow-hidden rounded-2xl border border-slate-700/60 bg-slate-950/30 px-3 py-3 shadow-[0_0_40px_rgba(15,23,42,0.55)]">
        <div className="mb-2 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-base font-semibold text-slate-200">
              {title}
            </p>
            {!collapsed && (
              <p className="mt-1 text-sm text-slate-400">{subtitle}</p>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => setSoundEnabled((value) => !value)}
              onKeyDown={stopControlKeyPropagation}
              className="grid h-8 w-8 place-items-center rounded-md border border-slate-700 bg-slate-900/70 text-slate-300 transition-colors hover:bg-slate-800 hover:text-slate-100"
              aria-label={soundEnabled ? "효과음 끄기" : "효과음 켜기"}
              title={soundEnabled ? "효과음 끄기" : "효과음 켜기"}
            >
              {soundEnabled ? (
                <Volume2 className="h-4 w-4" />
              ) : (
                <VolumeX className="h-4 w-4" />
              )}
            </button>
            <button
              type="button"
              onClick={() => setCollapsed((value) => !value)}
              onKeyDown={stopControlKeyPropagation}
              className="grid h-8 w-8 place-items-center rounded-md border border-slate-700 bg-slate-900/70 text-slate-300 transition-colors hover:bg-slate-800 hover:text-slate-100"
              aria-label={collapsed ? "게임 펼치기" : "게임 접기"}
              title={collapsed ? "게임 펼치기" : "게임 접기"}
            >
              {collapsed ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronUp className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>

        {collapsed ? (
          <div className="flex items-center justify-between rounded-lg border border-slate-700/70 bg-slate-900/45 px-3 py-2 text-sm text-slate-300">
            <span>게임 접힘</span>
            <span className="font-mono text-xs text-slate-400">
              SCORE {score}
            </span>
          </div>
        ) : (
          <>
            <canvas
              ref={canvasRef}
              width={gameWidth}
              height={GAME_HEIGHT}
              className="block w-full"
              style={{ height: GAME_HEIGHT }}
              onClick={() => wrapperRef.current?.focus()}
            />

            <div className="mt-2 text-center">
              <div className="mt-4 inline-flex flex-wrap items-center justify-center gap-2 rounded-lg border border-slate-600/70 px-4 py-2 text-sm text-slate-300">
                <span className="rounded border border-slate-500 px-2 py-0.5 font-mono text-xs">
                  Space
                </span>
                <span>점프</span>
                <span className="text-slate-500">|</span>
                <span>점수 {score}</span>
                {gameOver && (
                  <span className="text-slate-400">다시 시작 가능</span>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
