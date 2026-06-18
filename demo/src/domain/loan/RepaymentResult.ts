import { RepaymentGrade } from "./RepaymentGrade";

export type RepaymentResult = {
  grade: RepaymentGrade;
  amountPaid: number;
  remainingDebt: number;
  repaymentRate: number;  // 0.0 ~ 1.0
  trustDelta: number;
  stressDelta: number;
}
