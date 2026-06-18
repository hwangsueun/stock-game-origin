import { RepaymentGrade } from "../domain/loan/RepaymentGrade";
import type { RepaymentResult } from "../domain/loan/RepaymentResult";

interface GradeRule {
  grade: RepaymentGrade;
  minRate: number;   // 이상
  trustDelta: number;
  stressDelta: number;
}

const GRADE_RULES: GradeRule[] = [
  { grade: RepaymentGrade.EXCELLENT, minRate: 1.0,  trustDelta: +20, stressDelta: -15 },
  { grade: RepaymentGrade.GOOD,      minRate: 0.8,  trustDelta: +10, stressDelta: -8  },
  { grade: RepaymentGrade.NORMAL,    minRate: 0.5,  trustDelta:  0,  stressDelta:  0  },
  { grade: RepaymentGrade.BAD,       minRate: 0.01, trustDelta: -10, stressDelta: +10 },
  { grade: RepaymentGrade.DANGER,    minRate: 0,    trustDelta: -25, stressDelta: +25 },
];

export class RepaymentPolicy {
  /**
   * 상환 결과 계산
   * @param initialDebt 초기 대출금
   * @param amountPaid  실제 납부 금액
   * @param remainingDebt 납부 후 남은 빚
   */
  calculate(params: {
    initialDebt: number;
    amountPaid: number;
    remainingDebt: number;
  }): RepaymentResult {
    const { initialDebt, amountPaid, remainingDebt } = params;

    const rate = initialDebt > 0 ? Math.min(amountPaid / initialDebt, 1) : 1;
    const rule = GRADE_RULES.find((r) => rate >= r.minRate) ?? GRADE_RULES[GRADE_RULES.length - 1];

    return {
      grade: rule.grade,
      amountPaid,
      remainingDebt,
      repaymentRate: rate,
      trustDelta: rule.trustDelta,
      stressDelta: rule.stressDelta,
    };
  }

  /** 상환 필요액 (= 초기 대출금, 이 데모에서는 전액 기준) */
  requiredAmount(initialDebt: number): number {
    return initialDebt;
  }
}
