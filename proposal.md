# Homework 1 — Question 3

## MSBD5017 — Initial Project Scope Summary

### Team Members
- LIN Yue
- JIN Shengbo
- CHEN Yixuan

### Track
**Trade**

### Chosen Topic
**Blockchain-Native AI Research Agent for Crypto Quant**

---

## Core Research Question

To what extent does equipping an AI research agent with on-chain data-query and calculation tools improve the **claim-level accuracy, traceability, and reproducibility** of its market reports, and at which levels of question complexity — **direct lookup, derived metric, and multi-step inference** — does that advantage widen or disappear?

---

## Background

Blockchain records contain information about trading activity and liquidity, but interpreting these records can be time-consuming. AI can help summarize the data, yet it may make calculation errors or unsupported claims.

This project will build a small research agent that links its analysis to identifiable blockchain records and reproducible calculations.

---

## Project Boundaries and Methods

We will focus on **one Uniswap liquidity pool on Ethereum** over a fixed historical period, subject to data availability.

We will collect swap and liquidity events and calculate a small set of indicators, such as:

- Daily trading volume
- Trade counts
- Large-trade frequency

The definitions and thresholds for these indicators will be stated clearly.

The agent will use data-retrieval and calculation tools to answer predefined research questions and generate short reports. Each report will include:

- Observation period
- Relevant block numbers
- Data sources
- Calculation methods

The reports will also distinguish **measured findings** from **possible explanations**.

We will compare the tool-equipped agent with the **same AI model given the same source data but without access to calculation tools**.

Using a fixed set of questions and independently checked reference answers, we will evaluate:

- Numerical accuracy
- Source traceability
- Unsupported claims
- Report-generation time

Data snapshots, tool settings, and generated reports will be retained for verification.

We will also demonstrate recording their digital fingerprints, or **hashes**, on an Ethereum testnet to detect later modifications. This mechanism establishes **record integrity**, but does not by itself guarantee the correctness of the analysis.

The project will **not** include:

- Live trading
- Personalized investment advice
- Price-prediction guarantees
- Coverage of multiple blockchains
- Training a large AI model from scratch

Instead, the project will use an existing AI model and focus on tool-assisted blockchain data analysis and verification.

---

## Planned Deliverables

The planned deliverables include:

1. A documented on-chain dataset
2. A working research-agent prototype
3. Sample reports with traceable evidence
4. An Ethereum testnet hash-recording demonstration
5. An evaluation report describing accuracy, traceability, reproducibility, and limitations

---

## Preliminary References

1. MSBD5017, *Project Tracks – Data – Trade – Token*, Trade Topic 2.
2. Ethereum Documentation, *JSON-RPC API*.
3. Uniswap Documentation.
4. Ethereum Documentation, *Block Explorers*.
5. Yao, S. et al. (2023). *ReAct: Synergizing Reasoning and Acting in Language Models*. ICLR 2023.
6. Schick, T. et al. (2023). *Toolformer: Language Models Can Teach Themselves to Use Tools*. NeurIPS 2023.