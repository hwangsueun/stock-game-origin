import { create } from "zustand";
import { GameSession } from "../../domain/game/GameSession";
import type { GameSnapshot } from "../../application/dto/GameSnapshot";
import { buildSnapshot } from "../../application/dto/GameSnapshotBuilder";
import { GameSessionFactory } from "../../application/GameSessionFactory";
import { SupabaseGameDataRepository } from "../../infrastructure/storage/SupabaseGameDataRepository";
import { TradingService } from "../../services/TradingService";
import { InvestmentTurnService } from "../../services/InvestmentTurnService";
import { RepaymentService } from "../../services/RepaymentService";
import type { RepaymentResult } from "../../domain/loan/RepaymentResult";

// 서비스 인스턴스 (store 외부 — 재생성 불필요)
const repo            = new SupabaseGameDataRepository();
const factory         = new GameSessionFactory(repo);
const tradingService  = new TradingService();
const turnService     = new InvestmentTurnService();
const repayService    = new RepaymentService();

interface GameStore {
  // 상태
  session:  GameSession | null;
  snapshot: GameSnapshot | null;
  loading:  boolean;
  error:    string | null;

  // 액션
  initGame:    () => Promise<void>;
  startInvestment: () => void;
  buy:         (assetId: string, amount: number) => void;
  sellAll:     (assetId: string) => void;
  hold:        () => void;
  endTurn:     () => void;
  repay:       (amount: number) => RepaymentResult;
}

export const useGameStore = create<GameStore>((set, get) => ({
  session:  null,
  snapshot: null,
  loading:  false,
  error:    null,

  // ─── 초기 로드 ────────────────────────────────────────────
  initGame: async () => {
    set({ loading: true, error: null });
    try {
      const session = await factory.create();
      set({ session, snapshot: buildSnapshot(session), loading: false });
    } catch (e) {
      set({ error: String(e), loading: false });
    }
  },

  // ─── 헬퍼 ────────────────────────────────────────────────
  _sync: () => {
    const { session } = get();
    if (session) set({ snapshot: buildSnapshot(session) });
  },

  // ─── Phase 전환 ──────────────────────────────────────────
  startInvestment: () => {
    const { session } = get();
    if (!session) return;
    session.startInvestment();
    set({ snapshot: buildSnapshot(session) });
  },

  // ─── 거래 ────────────────────────────────────────────────
  buy: (assetId, amount) => {
    const { session } = get();
    if (!session) return;
    tradingService.buyByAmount(session, assetId, amount);
    set({ snapshot: buildSnapshot(session) });
  },

  sellAll: (assetId) => {
    const { session } = get();
    if (!session) return;
    tradingService.sellAll(session, assetId);
    set({ snapshot: buildSnapshot(session) });
  },

  // ─── 턴 ─────────────────────────────────────────────────
  hold: () => {
    const { session } = get();
    if (!session) return;
    turnService.hold(session);
    set({ snapshot: buildSnapshot(session) });
  },

  endTurn: () => {
    const { session } = get();
    if (!session) return;
    turnService.endTurn(session);
    set({ snapshot: buildSnapshot(session) });
  },

  // ─── 상환 ────────────────────────────────────────────────
  repay: (amount) => {
    const { session } = get();
    if (!session) throw new Error("session이 없습니다");
    const result = repayService.repay(session, amount);
    set({ snapshot: buildSnapshot(session) });
    return result;
  },
}));
