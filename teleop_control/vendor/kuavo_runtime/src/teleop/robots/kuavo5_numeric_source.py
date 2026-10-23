"""Embedded C++ for the user-started private official-math worker.

Derived from the numerically verified 20260920 RPC adapter; no solver rewrite.
The official wrapper can return success after a failed refinement; the existing
Python FK/limit/continuity checks remain mandatory. No hardware I/O on import.
"""

SOURCE = r'''
// Pure computation adapter; no ROS/control APIs. Official wrapper preserved.
#include <algorithm>
#include <array>
#include <cmath>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include "motion_capture_ik/json.hpp"
using Json = nlohmann::json;

struct Params {
  double optimality=1e-3, feasibility=1e-3, minor=1e-3, iterations=50;
  double orientation=11e-3, position=1e-3, weight=10;
  std::uint8_t mode=0;
  double elbow=0;
};
template<class V> Json numbers(const V& q) {
  Json result=Json::array();
  for(int i=0;i<q.size();++i) {
    if(std::isfinite(q[i]))result.push_back(q[i]);
    else result.push_back(nullptr);
  }
  return result;
}
template<class Vector, class Solve>
bool vendor_wrapper(const Vector& seed, Vector& output, const Params& exact,
                    Solve solve, Json& attempts) {
  auto attempt=[&](const Vector& q0,const Params& p,const char* stage) {
    const bool result=solve(q0,output,p);
    attempts.push_back({{"stage",stage},{"success",result},
        {"position_tolerance",p.position},{"orientation_tolerance",p.orientation},
        {"seed_rad",numbers(q0)},{"q_rad",numbers(output)}});
    return result;
  };
  auto coarse=exact;
  coarse.position=.020; coarse.orientation=.020;
  if(!attempt(seed,coarse,"coarse"))return false;
  const Vector coarse_seed=output;
  if(attempt(coarse_seed,exact,"exact"))return true;
  double low=coarse.position, high=exact.position;
  while(std::abs(high-low)>.002) {
    const double mid=(high+low)/2.;
    coarse.position=mid; coarse.orientation=mid;
    if(attempt(coarse_seed,coarse,"binary"))low=mid;
    else high=mid;
  }
  return true;
}

static std::vector<double> vector_field(const Json& j,int count) {
  if(!j.is_array() || j.size()!=count)throw std::invalid_argument("vector length");
  std::vector<double> out;
  for(const auto& value:j) {
    if(!value.is_number() || value.is_boolean())throw std::invalid_argument("numeric vector required");
    const double v=value.get<double>();
    if(!std::isfinite(v))throw std::invalid_argument("nonfinite vector");
    out.push_back(v);
  }
  return out;
}
#include "plantIK.h"
#include "drake/systems/framework/diagram_builder.h"
#include "drake/multibody/parsing/parser.h"
#include "drake/multibody/tree/fixed_offset_frame.h"

static HighlyDynamic::IKParams official_params(const Params& p) {
  HighlyDynamic::IKParams out;
  out.major_optimality_tol=p.optimality;
  out.major_feasibility_tol=p.feasibility;
  out.minor_feasibility_tol=p.minor;
  out.major_iterations_limit=p.iterations;
  out.oritation_constraint_tol=p.orientation;
  out.pos_constraint_tol=p.position;
  out.pos_cost_weight=p.weight;
  out.constraint_mode=p.mode;
  out.elbow_cost_scale=p.elbow;
  return out;
}
class NumericalPath {
 public:
  NumericalPath(const std::string& model,const Json& config,const std::string& full_model) {
    auto frames=config.at("end_frames_name_ik").get<std::vector<std::string>>();
    if(frames.size()!=5 || config.at("NUM_ARM_JOINT")!=14)
      throw std::invalid_argument("expected five fixed-base frames and fourteen arm joints");
    const double offset=config.at("eef_z_offset").get<double>();
    if(!std::isfinite(offset))throw std::invalid_argument("finite eef offset required");
    drake::multibody::MultibodyPlant<double> hardware(.001);
    drake::multibody::Parser(&hardware).AddModelFromFile(full_model);
    drake::systems::DiagramBuilder<double> builder;
    plant_=builder.AddSystem<drake::multibody::MultibodyPlant>(.001);
    drake::multibody::Parser(plant_).AddModelFromFile(model);
    plant_->WeldFrames(plant_->world_frame(),plant_->GetFrameByName(frames[0]));
    for(int i=0;i<2;++i) {
      const std::string custom=i==0?"eef_left":"eef_right";
      plant_->AddFrame(std::make_unique<drake::multibody::FixedOffsetFrame<double>>(
        custom,plant_->GetFrameByName(frames[i+1]),drake::math::RigidTransform<double>(
          drake::math::RotationMatrix<double>::Identity(),Eigen::Vector3d(0,0,offset))));
      frames[i+1]=custom;
    }
    plant_->Finalize();
    if(plant_->num_positions()!=14)throw std::invalid_argument("model must have exactly fourteen fixed-base DOF");
    for(int side=0;side<2;++side)for(int i=0;i<7;++i) {
      const auto name=std::string("zarm_")+(side==0?"l":"r")+std::to_string(i+1)+"_joint";
      if(plant_->GetJointByName(name).position_start()!=side*7+i)
        throw std::invalid_argument("Drake ordering differs from arm wire order");
      const auto& actual_joint=hardware.GetJointByName(name);
      if(actual_joint.num_positions()!=1)throw std::invalid_argument("hardware arm joint must have one position");
      hardware_lower_[side*7+i]=actual_joint.position_lower_limits()[0];
      hardware_upper_[side*7+i]=actual_joint.position_upper_limits()[0];
    }
    diagram_=builder.Build();
    context_=diagram_->CreateDefaultContext();
    ik_=std::make_unique<HighlyDynamic::CoMIK>(plant_,frames);
  }
  Json rpc(const Json& message) {
    if(message.at("op")=="fk") {
      const auto raw=vector_field(message.at("q"),14);
      Eigen::VectorXd q(14);for(int i=0;i<14;++i)q[i]=raw[i];
      Json result=Json::object();
      for(int side=0;side<2;++side) {
        const auto fk=ik_->FK(q,side==0?HighlyDynamic::HandSide::LEFT:HighlyDynamic::HandSide::RIGHT);
        result[side==0?"left":"right"]={{"position_m",numbers(fk.first)},
          {"orientation_xyzw",numbers(fk.second.coeffs())},{"frame_id","base_link"}};
      }
      return result;
    }
    if(message.at("op")!="ik")throw std::invalid_argument("only numerical fk/ik allowed");
    const auto& request=message.at("request");
    HighlyDynamic::FramePoseVec pose;
    pose.push_back({Eigen::Quaterniond(1,0,0,0),Eigen::Vector3d::Zero()});
    std::array<Eigen::Vector3d,2> elbows;
    Eigen::VectorXd seed(14);
    for(int side=0;side<2;++side) {
      const auto& h=request.at("hand_poses").at(side==0?"left_pose":"right_pose");
      const auto p=vector_field(h.at("pos_xyz"),3),q=vector_field(h.at("quat_xyzw"),4),a=vector_field(h.at("joint_angles"),7),e=vector_field(h.at("elbow_pos_xyz"),3);
      pose.push_back({Eigen::Quaterniond(q[3],q[0],q[1],q[2]),Eigen::Vector3d(p[0],p[1],p[2])});
      elbows[side]=Eigen::Vector3d(e[0],e[1],e[2]);for(int i=0;i<7;++i)seed[side*7+i]=a[i];
    }
    for(const auto& e:elbows)pose.push_back({Eigen::Quaterniond(1,0,0,0),e});
    Params p;
    if(request.at("use_custom_ik_param")==true) {
      const auto& c=request.at("ik_param");
      p.optimality=c.at("major_optimality_tol");p.feasibility=c.at("major_feasibility_tol");p.minor=c.at("minor_feasibility_tol");p.iterations=c.at("major_iterations_limit");
      p.orientation=c.at("oritation_constraint_tol");p.position=c.at("pos_constraint_tol");p.weight=c.at("pos_cost_weight");p.mode=c.at("constraint_mode").get<int>();p.elbow=c.at("elbow_cost_scale");
    }
    Json attempts=Json::array();Eigen::VectorXd result;
    const auto started=std::chrono::steady_clock::now();
    const bool ok=vendor_wrapper(seed,result,p,[&](const auto& initial,auto& q,const Params& params) {return ik_->solve(pose,initial,q,official_params(params));},attempts);
    const auto ended=std::chrono::steady_clock::now();
    return {{"success",ok},{"with_torso",false},{"q_torso",Json::array()},
      {"q_arm",ok?numbers(result):Json::array()},
      {"time_cost",std::chrono::duration<double,std::milli>(ended-started).count()}};
  }
 private:
  drake::multibody::MultibodyPlant<double>* plant_=nullptr;
  std::unique_ptr<drake::systems::Diagram<double>> diagram_;
  std::unique_ptr<drake::systems::Context<double>> context_;
  std::unique_ptr<HighlyDynamic::CoMIK> ik_;
  Eigen::VectorXd cache_,previous_output_;
  std::array<double,14> hardware_lower_{},hardware_upper_{};
  std::uint64_t epoch_=0,source_=0;
};


int main(int argc,char** argv) {
  try {
    if(argc!=4)throw std::invalid_argument("expected model, config and hardware model");
    std::ifstream file(argv[2]);Json config;file>>config;
    NumericalPath numeric(argv[1],config,argv[3]);
    std::cout<<"RPC_JSON "<<Json({{"event","ready"},{"dof",14},{"frame","base_link"}}).dump()<<std::endl;
    std::string line;
    while(std::getline(std::cin,line)) {
      if(line.size()>65536)throw std::invalid_argument("request too large");
      const auto packet=Json::parse(line);
      try {
        const auto result=numeric.rpc(packet.at("payload"));
        std::cout<<"RPC_JSON "<<Json({{"id",packet.at("id")},{"result",result}}).dump()<<std::endl;
      } catch(const std::exception& e) {
        std::cout<<"RPC_JSON "<<Json({{"id",packet.at("id")},{"error",e.what()}}).dump()<<std::endl;
      }
    }
    return 0;
  }catch(const std::exception& e) {std::cerr<<"NUMERIC_WORKER_ERROR "<<e.what()<<'\n';return 1;}
}
'''
