---

# **NeuralLift-360: Single-Image 3D Reconstruction**

### **A Forked and Extended Implementation of NeuralLift-360**

---

## **Table of Contents**
1. [Introduction](#introduction)
2. [Core Features](#core-features)
3. [Team Members and Contributions](#team-members-and-contributions)
4. [Methodology](#methodology)
5. [Installation](#installation)
6. [Usage Guide](#usage-guide)
7. [Experimental Results](#experimental-results)
8. [Optimized Hyperparameters](#optimized-hyperparameters)
9. [Google Colab Links](#google-colab-links)
10. [Acknowledgments and References](#acknowledgments-and-references)

---

## **Introduction**

NeuralLift-360 is a cutting-edge neural rendering framework designed to generate realistic 3D objects from single 2D images. This repository is a fork of the [original NeuralLift-360 implementation by VITA-Group](https://github.com/VITA-Group/NeuralLift-360), extended with significant enhancements for:
- Resource optimization on T4 GPUs.
- Improved reconstruction fidelity.
- Systematic hyperparameter tuning.

Our contributions also include a detailed analysis of performance trade-offs, enabling efficient deployment in constrained environments.

---

## **Core Features**
- **Realistic 3D Outputs**: Generates 3D objects with photorealistic textures and consistent geometry.
- **Optimized for Resource Constraints**: Adapted to T4 GPUs with 15GB VRAM.
- **Customizability**: Fine-tuned parameters for batch size, resolution, and rendering settings.
- **Ease of Use**: Modular design for seamless integration into existing workflows.

---

## **Team Members and Contributions**

| Name                 | GitHub Username              | Contribution                                    |
|----------------------|------------------------------|------------------------------------------------|
| **Aryan Sharma**     | [@ToxicCoder02](https://github.com/ToxicCoder02) | Project Lead: Comapatibliliy Testing ,Hyperparameter tuning, code optimization for Colab, and result validation. |
| **Sgailesh Chaudhary**    | [@Sschaudhary6](#)              | Documentation,Batch Tesing |

To verify individual contributions, refer to the `git log` in the repository.

---

## **Methodology**

1. **Fork and Extend the Original Code**:
   - Base Code: Forked from [VITA-Group/NeuralLift-360](https://github.com/VITA-Group/NeuralLift-360).
   - Repository Link: [ToxicCoder02/ML-Project](https://github.com/ToxicCoder02/ML-Project).

2. **Enhancements Made**:
   - Adjusted **batch size**, **resolution**, and **CLIP guidance weights** for computational efficiency.
   - Integrated dynamic VRAM usage monitoring to prevent overflows.
   - Fine-tuned **depth estimation algorithms** and **diffusion model parameters**.

3. **Version Control**:
   - All changes have been tracked in this repository. Use the `git log` command to view detailed commit histories.

---

## **Installation**

Fork or clone this repository:
```bash
git clone https://github.com/ToxicCoder02/ML-Project.git
cd ML-Project
pip install -r requirements.txt
```

To view the original code, visit [NeuralLift-360 by VITA-Group](https://github.com/VITA-Group/NeuralLift-360).

---

## **Usage Guide**

1. **Prepare Input**: Add your 2D image to the `inputs/` folder.
2. **Run the Pipeline**:
   ```bash
   python main.py --input inputs/sample.jpg --output outputs/
   ```
3. **Customize Parameters**:
   - Modify `config.yaml` to change hyperparameters such as batch size, resolution, and CLIP weights.
4. **View Outputs**: Results are saved in the `outputs/` folder, including RGB and depth maps.

---

## **Experimental Results**

### **Reconstruction Outputs**
#### Input Image
<img src="images/input_image.jpg" alt="Input Image" width="300">

#### Reconstructed RGB 360° View
<img src="images/rgb_360.gif" alt="RGB 360° View" width="500">

#### Reconstructed Depth 360° View
<img src="images/depth_360.gif" alt="Depth 360° View" width="500">

---

### **Training Efficiency**
#### GPU VRAM Usage vs. Resolution
| Training Resolution | VRAM Usage | Output Quality                  |
|----------------------|------------|----------------------------------|
| 32×32               | ~7 GB      | Blurry textures, oversimplified geometry |
| 128×128             | ~10 GB     | Clear textures, crisp geometry  |
| 256×256             | ~13 GB     | Excellent detail, slower convergence |

#### Loss vs. Batch Size
<img src="images/loss_vs_batch_size.png" alt="Loss vs Batch Size" width="500">

---

## **Optimized Hyperparameters**
| Parameter            | Optimized Value |
|----------------------|-----------------|
| Batch Size           | 256             |
| Training Resolution  | 128×128         |
| Rendering Resolution | 200×200         |
| CLIP Guidance Weight | 10              |

---

## **Google Colab Links**

Explore the pre-configured notebooks for experimentation:
1. [Quick Start Notebook](#)
2. [Advanced Reconstruction](#)
3. [Hyperparameter Tuning](#)
4. [Multi-Object Processing](#)
5. [Visualization and Testing](#)

---

## **Acknowledgments and References**

### **Acknowledgments**
- Original framework by [VITA-Group](https://github.com/VITA-Group/NeuralLift-360).
- GPU resources provided by Google Colab.

### **References**
1. [NeuralLift-360: GitHub Repository](https://github.com/VITA-Group/NeuralLift-360)
2. [CLIP: Learning Transferable Visual Models From Natural Language Supervision](https://arxiv.org/abs/2103.00020)
3. [NeRF: Neural Radiance Fields](https://arxiv.org/abs/2003.08934)
4. [NVIDIA T4 GPU Architecture](https://www.nvidia.com/en-us/data-center/tesla-t4/)

---
